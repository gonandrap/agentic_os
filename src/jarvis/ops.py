"""High-level operations shared by the CLI, the web UI, and the Jarvis persona"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import logging
import os
import re
import signal
import sqlite3
import subprocess
import sys
import time
from collections import Counter
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any, Iterable
from urllib.parse import quote

log = logging.getLogger("jarvis.ops")

if TYPE_CHECKING:  # pragma: no cover - typing only
    from . import landing
    from .holds import Hold

from .bootstrap import BootstrapReport, bootstrap_project, settings_drift
from .catalog import (
    DEFAULT_STAKES_CLASSIFIER,
    DEFAULT_VALIDATION_FOLLOW_UP_CAP,
    SAFETY_KEYS,
    Catalog,
    CatalogError,
    ProjectSpec,
    load_catalog,
    parse_catalog,
    worker_stalls_on_prompts,
)
from . import (budget, bus, concision, config_version, db, fleet, harvest, health,
               invariants, observability, release, rules, timeline)
from .release import RED_DEFER_EVENT, RED_PARK_EVENT
from .agent_usage import (
    OBSERVE_CONTEXT, OBSERVE_INSPECT, OBSERVE_LIVE, OBSERVE_WHY,
)
from .sections import QUESTION_MAX_CHARS, QUESTION_WARN_CHARS
from .central_store import MISSED_MIN_WORDS, SETTLED_BACKLOG_STATUSES, CentralStore
from .daemon import (
    OBJECTION_HANDED_BACK_REASON,
    OBJECTION_WITHDRAWN_REASON,
    RECONCILE_TICK_KEY,
    answers_question,
    daemon_running,
    delivers_message,
    polls_and_can_act_on_pull_request,
    polls_pull_request,
    retries_turn,
    reviews_gate,
    settles_turn,
    validation_round,
)
from .github import GitHubError
from .invariants import (
    PR_CLOSED_BLOCKER,
    UNLANDED_BLOCKER,
    pr_pending_blocker,
    true_blockers,
)
from .paths import daemon_pidfile, ensure_home, logs_dir
from .worker_brief import MODEL_ATTRIBUTION_NAMES
# `ops.IDLE_MANAGER_CAUSE` is an ALIAS: `stuck.IDLE_MANAGER_CAUSE` is the one definition of the cause... (#1008)
from .stuck import IDLE_MANAGER_CAUSE  # noqa: F401
from .project_store import (
    ALARM_REVIEWABLE_STATUSES,
    ASSUMPTION_DECIDER_OS,
    ASSUMPTION_DECIDER_USER,
    COUNTED_VALIDATION_OUTCOMES,
    FAMILY_CHILD_KINDS,
    FORCEABLE_STATUSES,
    FO_OPEN_STATUSES,
    FO_STATUSES,
    FO_TERMINAL_STATUSES,
    NO_TURN,
    OPEN_STATUSES,
    OPEN_VALIDATION_OUTCOMES,
    PR_PENDING_EVENT,
    SETTLE_BY_FINISH,
    SETTLE_IN_SESSION,
    TERMINAL_STATUSES,
    WORKER_CHILDREN,
    WO_STATUSES,
    ProjectStore,
    claim_sql_selects,
    decide_command,
    feature_status_label,
    is_feature_order_id,
    kind_contract,
    order_path,
    submission_artifacts,
    validation_standing,
)


class OpsError(RuntimeError):
    """User-facing operational error"""


# -- catalog resolution

def resolve_catalog(catalog_path: str | None = None) -> Catalog:
    """Load the catalog from an explicit path, or the one registered at start"""
    if catalog_path:
        return load_catalog(catalog_path)
    central = CentralStore()
    try:
        stored = central.get_state("catalog_path")
    finally:
        central.close()
    if not stored:
        raise OpsError(
            "no catalog registered — run `jarvis start --catalog <file>` first, "
            "or pass --catalog explicitly"
        )
    return load_catalog(stored)


def project_spec(catalog: Catalog, name: str) -> ProjectSpec:
    try:
        return catalog.project(name)
    except CatalogError as e:
        raise OpsError(str(e)) from e


def validation_enabled(project: str | None = None) -> bool:
    """Is validation on — for `project`, or fleet-wide?"""
    cfg = validation_config(project)
    return bool(cfg is not None and cfg.enabled)


# -- OS lifecycle

def start_os(catalog_path: str, force_config: bool = False,
             foreground: bool = False, poll_interval: float = 5.0) -> dict[str, Any]:
    """Validate the catalog, bootstrap every project, register them, start jarvisd"""
    from . import claude_cli

    catalog = load_catalog(catalog_path)
    ensure_home()

    if not claude_cli.available():
        raise OpsError("`claude` CLI not found on PATH — install Claude Code first")

    reports: list[BootstrapReport] = []
    central = CentralStore()
    try:
        for project in catalog.projects:
            report = bootstrap_project(project, force_config=force_config)
            reports.append(report)
            if not report.warnings or (project.path / ".jarvis").is_dir():
                central.upsert_project(
                    name=project.name,
                    path=str(project.path),
                    description=project.description,
                    model=project.model,
                    catalog_json=json.dumps(project.raw),
                )
        central.set_state("catalog_path", str(Path(catalog_path).expanduser().resolve()))
    finally:
        central.close()

    pid = daemon_running()
    if pid:
        daemon_info = {"status": "already-running", "pid": pid}
    elif foreground:
        daemon_info = {"status": "foreground"}
    else:
        proc = _spawn_daemon(catalog_path, poll_interval)
        time.sleep(1.0)
        if proc.poll() is not None:
            raise OpsError(
                f"jarvisd exited immediately (rc={proc.returncode}) — "
                f"check {logs_dir() / 'jarvisd.log'}"
            )
        daemon_info = {"status": "started", "pid": proc.pid}

    return {
        "projects": [
            {"name": r.project, "actions": r.actions, "warnings": r.warnings}
            for r in reports
        ],
        "daemon": daemon_info,
    }


def _spawn_daemon(catalog_path: str, poll_interval: float) -> subprocess.Popen:
    logs_dir().mkdir(parents=True, exist_ok=True)
    out = (logs_dir() / "jarvisd.out").open("a")
    return subprocess.Popen(
        [sys.executable, "-m", "jarvis.cli", "daemon", "run",
         "--catalog", str(Path(catalog_path).expanduser().resolve()),
         "--poll-interval", str(poll_interval)],
        stdout=out, stderr=out, stdin=subprocess.DEVNULL,
        start_new_session=True,  # detach from the terminal
    )


def fleet_summary(central: CentralStore) -> dict[str, Any]:
    """The user's pause and the post-reopen ramp, for `jarvis status` (issue #843)"""
    pause = fleet.load_pause(central)
    ramp = fleet.ramp(central)
    return {
        "paused": pause is not None,
        "since": pause.since if pause else None,
        "reason": pause.reason if pause else "",
        "allow": sorted(pause.allow) if pause else [],
        "ramp": ({"since": ramp.since, "until": ramp.until, "cap": ramp.cap,
                  "tripped": ramp.tripped} if ramp else None),
    }


def pause_fleet(reason: str = "", allow: list[str] | None = None) -> dict[str, Any]:
    """`jarvis pause`: no work order starts a turn unless it is allow-listed"""
    central = CentralStore()
    try:
        ids = [_resolve_order_id(i) for i in (allow or [])]
        pause = fleet.pause(central, reason=reason, allow=ids)
        return {"paused": True, "since": pause.since, "reason": pause.reason,
                "allow": sorted(pause.allow)}
    finally:
        central.close()


def resume_fleet(order_ids: list[str] | None = None,
                 everything: bool = False) -> dict[str, Any]:
    """`jarvis resume`: let named orders through a pause, or lift it with `--all`"""
    central = CentralStore()
    try:
        if everything:
            fleet.unpause(central)
            return {"paused": False}
        if not order_ids:
            raise OpsError("name the work orders to let through, or pass --all to lift "
                           "the pause for the whole fleet")
        try:
            pause = fleet.allow(central, [_resolve_order_id(i) for i in order_ids])
        except ValueError as e:
            raise OpsError(str(e)) from e
        return {"paused": True, "since": pause.since, "reason": pause.reason,
                "allow": sorted(pause.allow)}
    finally:
        central.close()


def _resolve_order_id(order_id: str) -> str:
    """A work or feature order id that exists — a typo must not silently allow nothing"""
    # `project_store.is_feature_order_id` (#997)
    if is_feature_order_id(order_id):
        find_feature_order(order_id)
    else:
        find_work_order(order_id)
    return order_id


def stop_os() -> dict[str, Any]:
    pid = daemon_running()
    if not pid:
        return {"status": "not-running"}
    os.kill(pid, signal.SIGTERM)
    for _ in range(50):
        if daemon_running() is None:
            return {"status": "stopped", "pid": pid}
        time.sleep(0.1)
    return {"status": "still-stopping", "pid": pid}


# -- status

def run_doctor(project: str | None = None, repair: bool = False,
               catalog_path: str | None = None,
               include_os: bool = True) -> dict[str, Any]:
    """Run the OS's post-condition checks over one project or the whole fleet (#232)"""
    from .invariants import check_catalog, check_os, check_project, check_release_marker

    # Catalog-level checks run whenever a catalog is resolvable at all: a gate that can never open is a...
    config_violations: dict[str, list[Any]] = {}
    try:
        cat = resolve_catalog(catalog_path)
    except (OpsError, CatalogError):
        cat = None
    if cat is not None:
        for v in check_catalog(cat, project):
            config_violations.setdefault(v.context.get("project", ""), []).append(v)

    if catalog_path:
        # Explicit catalog: works before the OS has ever been started
        rows = [{"name": ps.name, "path": str(ps.path), "status": "active"}
                for ps in resolve_catalog(catalog_path).projects]
    else:
        central = CentralStore()
        try:
            rows = central.list_projects()
        finally:
            central.close()
    if project:
        rows = [p for p in rows if p["name"] == project]
        if not rows:
            raise OpsError(f"unknown project: {project}")

    # OS-level checks first: they are about the OS itself (is the dashboard alive?)
    os_found = check_os() if include_os else []
    results, total = [], len(os_found)
    for p in rows:
        if p["status"] != "active":
            continue
        path = Path(p["path"])
        if not path.is_dir():
            results.append({"project": p["name"], "error": "path missing",
                            "violations": []})
            continue
        store = ProjectStore(path)
        try:
            # `slow=True` unconditionally: INV-WORK-LANDED is the whole reason issue #232 asked for a report
            found = check_project(store, repair=repair, slow=True)
        finally:
            store.close()
        found = [*config_violations.pop(p["name"], []), *found]
        total += len(found)
        results.append({
            "project": p["name"],
            "violations": [
                {"invariant": v.invariant, "wo_id": v.wo_id, "detail": v.detail,
                 "repaired": v.repaired, "repair": v.repair}
                for v in found
            ],
        })
    # A catalog project the registry doesn't know about still gets its config reported
    for name, violations in config_violations.items():
        total += len(violations)
        results.append({
            "project": name,
            "violations": [
                {"invariant": v.invariant, "wo_id": v.wo_id, "detail": v.detail,
                 "repaired": v.repaired, "repair": v.repair}
                for v in violations
            ],
        })
    # OS-level state under $JARVIS_HOME, owned by no project: a pending-release marker stuck in flight
    os_violations = check_release_marker() if include_os else []
    if os_violations:
        total += len(os_violations)
        results.append({
            "project": "(os)",
            "violations": [
                {"invariant": v.invariant, "wo_id": v.wo_id, "detail": v.detail,
                 "repaired": v.repaired, "repair": v.repair}
                for v in os_violations
            ],
        })
    out = {
        "repair": repair,
        "include_os": include_os,
        "violations": total,
        "os": [{"invariant": v.invariant, "detail": v.detail, "repaired": v.repaired,
                "repair": v.repair, "context": v.context} for v in os_found],
        "projects": results,
    }
    orphans = orphaned_worker_sessions()
    if orphans:
        out["orphaned_sessions"] = orphans
    return out


def orphaned_worker_sessions() -> list[dict[str, Any]]:
    """Background agents named `[WO …]` that no open work order is driving"""
    from . import claude_cli

    if not claude_cli.available():
        return []
    try:
        sessions = claude_cli.list_background_sessions()
    except claude_cli.ClaudeCliError:
        return []
    named = [s for s in sessions if s.name.startswith("[WO ")]
    if not named:
        return []
    # A STOPPED worker session holds nothing: every closed background-transport order leaves one (docs/superpowers/specs/2026-10-22-workers-on-background-sessions.md)
    try:
        roster = claude_cli.bg_roster()
    except claude_cli.ClaudeCliError:
        roster = {}
    named = [s for s in named
             if not (s.session_id in roster and not roster[s.session_id].alive
                     and roster[s.session_id].state in claude_cli.FINISHED_STATES)]

    live_sessions: set[str] = set()
    for name, path in registered_project_paths().items():  # noqa: B007
        if not path.is_dir():
            continue
        store = ProjectStore(path)
        try:
            for wo in store.list_work_orders(statuses=OPEN_STATUSES,
                                             include_hidden=True):
                if wo.get("session_id"):
                    live_sessions.add(wo["session_id"])
        finally:
            store.close()
    return [
        {"bg_id": s.id, "session_id": s.session_id, "name": s.name, "state": s.state,
         "stop": f"claude stop {s.id}"}
        for s in named if s.session_id not in live_sessions
    ]


def ui_health() -> dict[str, Any]:
    """How the dashboard is doing, from its own log on disk"""
    from . import uilog

    recent, total = uilog.recent_errors()
    return {
        "errors": total,
        "window_seconds": uilog.ERROR_WINDOW_SECONDS,
        "log": str(uilog.ui_log_path()),
        "access_log": str(uilog.access_log_path()),
        "recent": [{**e.as_dict(), "summary": e.summary} for e in recent],
    }


def _neo_attention() -> tuple[dict[str, int], list[dict[str, Any]]]:
    """`(counts, questions Neo handed back to the user)` — one open of Neo's DB"""
    from .neo_store import NeoStore

    neo = NeoStore()
    try:
        return (neo.counts(),
                [q for q in neo.list_questions(statuses=("escalated", "failed"))
                 if q.get("kind") not in ("approval", "plan", "alarm", "assumption",
                                          # its improvement order carries the attention
                                          "findings")])
    finally:
        neo.close()


_RETRIES_SUFFIX = re.compile(r"\s*\(after \d+ retries — nobody has judged this\)$")


def fold_neo_attention(questions: list[dict[str, Any]],
                       rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per CAUSE with a count, biggest first: one outage is one thing to fix"""
    from .neo_store import REVIVABLE_KINDS, UNREACHABLE_PREFIX

    groups: dict[tuple[str, ...], list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for q, row in zip(questions, rows):
        reason = q.get("answer_reason") or ""
        if q["status"] == "failed" and reason.startswith(UNREACHABLE_PREFIX):
            # Unjudged: the outage is the subject, whatever each question asked
            key = (q["project"], q.get("kind") or "", "unreachable",
                   _RETRIES_SUFFIX.sub("", reason))
        else:
            # Judged: only the same question asked twice is the same decision
            key = (q["project"], q.get("kind") or "", q.get("wo_id") or "", q["question"])
        groups.setdefault(key, []).append((q, row))
    out = []
    for key, members in groups.items():
        q, row = members[-1]
        ids = [m["id"] for m, _ in members]
        row = {**row, "count": len(ids), "neo_question_ids": ids}
        if len(ids) > 1 and key[2] == "unreachable":
            kind = q.get("kind") or "question"
            heals = (" — they re-ask themselves once Neo answers again; `jarvis neo list` "
                     "to read them" if kind in REVIVABLE_KINDS
                     else " — `jarvis neo list` to read and answer them")
            row.update(title=f"Neo could not be reached for {len(ids)} {kind} questions",
                       reason=_RETRIES_SUFFIX.sub("", q.get("answer_reason") or "") + heals,
                       wo_id=None, decide="jarvis neo list")
        elif len(ids) > 1:
            row["title"] = f"{row['title']} (asked {len(ids)}×)"
        out.append(row)
    return sorted(out, key=lambda r: -r["count"])


def _held_jobs(store: ProjectStore, catalog: Catalog | None = None) -> list[dict[str, Any]]:
    """Scheduled jobs this project is STILL SUPPOSED TO RUN and cannot"""
    cfg = schedule_config_at(store.project_path, catalog)
    if not cfg.enabled:
        return []
    return [{"job_id": st["job_id"], "since": st["held_since"],
             "reason": st["held_reason"] or "", "wo_id": st["last_wo_id"]}
            for st in store.list_schedule_states()
            if st["held_since"] and st["job_id"] in cfg.jobs]


#: §2b: dark once the newest stamp is older than max(MIN_SECONDS, interval * INTERVALS)
DAEMON_DARK_INTERVALS = 10
DAEMON_DARK_MIN_SECONDS = 300


def _daemon_liveness(central: CentralStore, pid: int | None) -> dict[str, Any] | None:
    """ONE fleet-level item when jarvisd is stopped or dark, else None (docs/superpowers/specs/2026-10-16-a-stopped-daemon-is-a-liveness-gap.md)"""
    if pid is None:
        path = central.get_state("catalog_path")
        return {
            "project": "os", "wo_id": None,
            "title": "jarvisd is not running", "status": "daemon",
            "reason": "nothing is dispatching work orders, relaunching turns, "
                      "delivering messages or settling any work order",
            "cause": "stopped",
            "decide": f"jarvis start --catalog {path}" if path else "jarvis start",
        }
    stamp = db.from_json(central.get_state(RECONCILE_TICK_KEY), None)
    ts = interval = 0.0
    if isinstance(stamp, dict):
        try:
            ts = float(stamp.get("ts") or 0.0)
            interval = float(stamp.get("interval") or 0.0)
        except (TypeError, ValueError):  # a malformed stamp is no stamp
            ts = interval = 0.0
    if not ts:
        # §2b: with no stamp the age is measured from the start, and with neither nothing fires
        interval = 0.0
        try:
            ts = float(central.get_state("daemon_started_at") or 0.0)
        except (TypeError, ValueError):
            ts = 0.0
        if not ts:
            return None
    dark = db.now() - ts
    if dark < max(float(DAEMON_DARK_MIN_SECONDS), interval * DAEMON_DARK_INTERVALS):
        return None
    # No `decide`: no jarvis verb restarts a wedged daemon (§2b)
    return {
        "project": "os", "wo_id": None,
        "title": "jarvisd is dark", "status": "daemon",
        "reason": f"pid {pid} is alive but has completed no reconcile pass for "
                  f"{int(dark / 60)}m — nothing is being dispatched, relaunched, "
                  f"delivered or settled",
        "cause": "dark",
    }


def os_status(catalog: Catalog | None = None) -> dict[str, Any]:
    central = CentralStore()
    try:
        pid = daemon_running()
        projects = []
        attention: list[dict[str, Any]] = []
        # Two things off the catalog
        fleet_state = None
        try:
            _cat = catalog or resolve_catalog()
            mode_by_project = {ps.name: ps.worker.permission_mode for ps in _cat.projects}
            fleet_state = fleet.current(_cat, central=central)
        except (OpsError, CatalogError):
            mode_by_project = {}
            # `_cat` stays None and `_held_jobs` resolves the catalog itself
            _cat = None
        # Read Neo's questions BEFORE the project loop
        neo_counts, escalated_questions = _neo_attention()
        neo_held = {q["wo_id"] for q in escalated_questions}
        for p in central.list_projects():
            if p["status"] != "active":
                continue
            path = Path(p["path"])
            if not path.is_dir():
                projects.append({**p, "error": "path missing"})
                continue
            store = ProjectStore(path)
            try:
                summary = store.summary()
                open_wos = store.list_work_orders(statuses=OPEN_STATUSES)
                # Attention isn't limited to open work orders: a FAILED worker (e.g. session disappeared) still needs...
                flagged = {wo["id"]: wo for wo in open_wos if wo["needs_attention"]}
                for wo in store.list_work_orders():
                    if wo["needs_attention"]:
                        flagged.setdefault(wo["id"], wo)
                # Work orders held up by an escalated gate are reported once
                gate_held = {a["wo_id"] for a in store.escalated_approvals()}
                # Derived once for the whole strip: a flagged order can be parked AND owe the user something else
                parked_by_id = {}
                for wo in flagged.values():
                    reason = invariants.parked_reason(store, wo)
                    if reason:
                        parked_by_id[wo["id"]] = reason
                # A feature order contributes ONE line to the strip, never one per child
                rolled_up: dict[str, list[dict[str, Any]]] = {}
                for wo in flagged.values():
                    if wo["id"] in gate_held or wo["id"] in neo_held:
                        continue
                    if wo.get("parent_id"):
                        rolled_up.setdefault(wo["parent_id"], []).append(wo)
                        continue
                    item = {
                        "project": p["name"], "wo_id": wo["id"],
                        "title": wo["title"], "status": wo["status"],
                        "reason": wo["attention_reason"],
                    }
                    # …and the half the flag could not carry
                    parked = parked_by_id.get(wo["id"])
                    if parked and parked != wo["attention_reason"]:
                        item["parked"] = parked
                    # A worker blocked on a permission prompt can't be approved from jarvis
                    if wo["status"] == "waiting_input" and wo["session_id"]:
                        item["attach"] = f"claude --resume {wo['session_id']}"
                        # …and `jarvis wo resume-auto` ONLY where a prompt is possible at all
                        mode = mode_by_project.get(p["name"])
                        if mode and worker_stalls_on_prompts(mode):
                            item["resume_auto"] = f"jarvis wo resume-auto {wo['id']}"
                    attention.append(item)
                # `kind=None`: KIND-AGNOSTIC PATH (docs/superpowers/specs/2026-10-01-a-family-capped-raise-must-say-so.md)
                features = store.list_feature_orders(statuses=FO_OPEN_STATUSES,
                                                     kind=None)
                # Which feature orders get a line: the open ones
                by_id = {fo["id"]: fo for fo in features}
                for fo in store.flagged_feature_orders():
                    by_id.setdefault(fo["id"], fo)
                for parent_id in rolled_up:
                    if parent_id not in by_id:
                        try:
                            by_id[parent_id] = store.get_feature_order(parent_id)
                        except KeyError:  # deleted out from under its children
                            by_id[parent_id] = {}
                for fo_id, fo in by_id.items():
                    # THE ACCESSOR `feature_progress` reads, so the label and the rollup agree
                    owed = {k["id"] for k in rolled_up.get(fo_id, [])}
                    kids = [k for k in store.feature_children(fo_id, kinds=None)
                            if k["id"] in owed]
                    if not fo or not (fo["needs_attention"] or kids):
                        continue
                    reasons = []
                    if fo["needs_attention"] and fo["attention_reason"]:
                        reasons.append(fo["attention_reason"])
                    if kids:
                        reasons.append(
                            f"{len(kids)} of its work orders need you: "
                            + ", ".join(_child_note(k, parked_by_id) for k in kids)
                        )
                    progress = feature_progress(store, fo)
                    # §6.2 of docs/superpowers/specs/2026-09-23-improvement-orders.md: an improvement order's reason is...
                    improvement = (fo.get("kind") or "feature") == "improvement"
                    body = "; ".join(reasons)
                    attention.append({
                        "project": p["name"], "wo_id": None, "fo_id": fo_id,
                        "title": fo["title"],
                        # Kind-aware through the ONE mapping in project_store (docs/superpowers/specs/2026-09-23-improvement-orders.md)
                        "status": (f"{fo.get('kind') or 'feature'}:"
                                   f"{feature_status_label(fo.get('kind'), fo['status'])}"),
                        "reason": body if improvement
                                  else f"{progress['label']} — {body}",
                        "rolled_up": [k["id"] for k in kids],
                        "decide": decide_command(fo.get("kind"), fo_id),
                    })
                drift = settings_drift(path / ".claude" / "settings.json")
                projects.append({
                    "name": p["name"], "path": p["path"],
                    "description": p["description"],
                    "summary": summary,
                    # ONE MERGED LIST WITH A `kind` LABEL
                    "feature_orders": [
                        {**{k: fo[k] for k in ("id", "title", "status",
                                               "needs_attention", "attention_reason")},
                         "kind": fo.get("kind") or "feature",
                         "status_label": feature_status_label(fo.get("kind"),
                                                              fo["status"]),
                         "progress": feature_progress(store, fo)}
                        for fo in features
                    ],
                    "open_work_orders": [
                        {**{k: wo[k] for k in ("id", "title", "status", "origin",
                                               "needs_attention", "attention_reason",
                                               "pr_url")},
                         # Why a pending work order is not starting
                         "blocked_by": blocked_by(store, wo),
                         # Same rule for the other kind of hold — §2.7
                         "preconditions": open_preconditions(store, wo),
                         # Same rule, for the other reason a work order can be sitting still: the transport dropped its turn
                         "pause": invariants.pause_note(store, wo),
                         # And the whole answer in one string
                         "status_label": invariants.status_label(store, wo, fleet_state)}
                        for wo in open_wos
                    ],
                    "settings_drift": drift,
                    # ONLY THE HELD ONES
                    "schedule_held": _held_jobs(store, _cat),
                })
                if drift:
                    attention.append({
                        "project": p["name"], "wo_id": None,
                        "title": "settings drift", "status": "config",
                        "reason": f".claude/settings.json: {drift}",
                    })
                # A STANDING CRITICAL VIOLATION IS AN ATTENTION ITEM — Neo 1084
                for report in store.standing_violations(level="critical"):
                    # …EXCEPT ONE THAT OWES A DECISION: the work order's own attention flag already carries it
                    if report.get("owed"):
                        continue
                    attention.append({
                        "project": p["name"], "wo_id": report["wo_id"] or None,
                        "title": f"OS invariant violated: {report['invariant']}",
                        "status": "invariant", "invariant": report["invariant"],
                        "reason": report["detail"] or report["invariant"],
                        "decide": f"jarvis doctor {p['name']}",
                    })
                mode = mode_by_project.get(p["name"])
                if mode and worker_stalls_on_prompts(mode):
                    attention.append({
                        "project": p["name"], "wo_id": None,
                        "title": "worker permission mode", "status": "config",
                        "reason": f"workers run in '{mode}' — a background worker can't "
                                  "answer permission prompts and will stall; set "
                                  "permission_mode to 'auto'",
                    })
            finally:
                store.close()
        inbox = central.unacked_inbox()
        backlog_open = central.list_backlog(status="open")
        neo_rows = []
        for q in escalated_questions:
            # A `triage` question is the one kind with NO WORKER BEHIND IT (issue #240): nothing is blocked on...
            triage = q.get("kind") == "triage"
            url = ""
            if triage:
                from . import issues
                url = (issues.triage_payload(q) or {}).get("issue_url") or ""
            neo_rows.append({
                "project": q["project"], "wo_id": q["wo_id"],
                "title": (f"Neo could not confirm a bug's priority: {q['question'][:60]}"
                          if triage else f"Neo escalated: {q['question'][:80]}"),
                "status": "neo_escalated",
                "reason": q.get("answer_reason") or (
                    "the rating is UNCONFIRMED, so nothing was dispatched and no "
                    "release was cut" if triage
                    else "Neo declined to answer for you"),
                "neo_question_id": q["id"],
                "decide": (issues.START_COMMAND.format(url=url) if url else
                           f"jarvis neo show {q['id']}") if triage else
                          f"jarvis neo answer {q['id']} \"…\"",
            })
        attention.extend(fold_neo_attention(escalated_questions, neo_rows))
        # Gates Neo sent up
        gate_items = []
        # Gate requests that turned out not to be gated actions at all
        false_positives = 0
        # Held requests whose worker never came back
        abandoned = 0
        for name, path in registered_project_paths().items():
            if not path.is_dir():
                continue
            store = ProjectStore(path)
            try:
                false_positives += store.dismissed_count()
                abandoned += store.abandoned_count()
                for a in store.escalated_approvals():
                    gate_items.append({
                        "project": name, "wo_id": a["wo_id"],
                        "title": f"approve {a['kind']}: {a['command'][:60]}",
                        "status": "gate_escalated",
                        "reason": a["escalation_reason"] or "Neo declined to decide",
                        "approval_id": a["id"],
                        "decide": f"jarvis gate approve {a['id']} --reason \"...\"",
                    })
            finally:
                store.close()
        attention.extend(gate_items)
        # The dashboard is part of the OS, so its failures belong in the OS's pulse
        ui = ui_health()
        if ui["errors"]:
            attention.append({
                "project": "os", "wo_id": None,
                "title": "dashboard errors", "status": "ui",
                "reason": f"{ui['errors']} unhandled error"
                          f"{'s' if ui['errors'] != 1 else ''} in the last "
                          f"{int(ui['window_seconds'] / 3600)}h — latest: "
                          f"{ui['recent'][0]['summary']}. Full traceback: {ui['log']}",
            })
        # Beside it, under the same argument: the daemon is the OS (docs/superpowers/specs/2026-10-16-a-stopped-daemon-is-a-liveness-gap.md)
        liveness = _daemon_liveness(central, pid)
        if liveness:
            attention.append(liveness)
        return {
            "daemon": {
                "running": pid is not None,
                "pid": pid,
                "catalog": central.get_state("catalog_path"),
            },
            # The user's brake and the reopen ramp (issue #843)
            "fleet": fleet_summary(central),
            "ui": ui,
            "projects": projects,
            "attention": attention,
            "inbox": {
                "unacked": len(inbox),
                "critical": sum(1 for i in inbox if i["level"] == "critical"),
                "items": inbox[:10],
            },
            "backlog": {"open": len(backlog_open)},
            "neo": neo_counts,
            "gates": {"awaiting_you": len(gate_items),
                      "false_positives": false_positives,
                      "abandoned": abandoned},
            "healthy": pid is not None and not attention,
        }
    finally:
        central.close()


# -- work orders

def _child_note(wo: dict[str, Any], parked: dict[str, str]) -> str:
    """One rolled-up child on a feature order's attention line"""
    note = f"{wo['id']} ({wo['attention_reason']})"
    extra = parked.get(wo["id"])
    return f"{note} — {extra}" if extra and extra != wo["attention_reason"] else note


def registered_project_paths() -> dict[str, Path]:
    central = CentralStore()
    try:
        return {p["name"]: Path(p["path"]) for p in central.list_projects()
                if p["status"] == "active"}
    finally:
        central.close()


MODEL_ALIASES = ("opus", "sonnet", "haiku", "fable")
_MODEL_ID_RE = re.compile(r"(?:opus|sonnet|haiku|fable"
                          r"|claude-(?:opus|sonnet|haiku|fable)-\d+(?:-\d+)*(?:-\d{8})?)"
                          r"(?:\[1m\])?")


def suggested_model_ids() -> list[str]:
    """What the create forms offer: the aliases, then every id the OS knows by name"""
    return [*MODEL_ALIASES, *MODEL_ATTRIBUTION_NAMES]


def _model_shaped(model: str) -> bool:
    return bool(_MODEL_ID_RE.fullmatch(model)) or model in MODEL_ATTRIBUTION_NAMES


def check_model_id(model: str | None) -> None:
    """Refuse a model id claude would reject, before anything is filed or relaunched"""
    import difflib

    if not model or _model_shaped(model):
        return
    dashed = model.replace(".", "-")
    hint = ([dashed] if _model_shaped(dashed)
            else difflib.get_close_matches(model, suggested_model_ids(), n=1))
    raise OpsError(f"unknown model id {model!r}"
                   + (f" — did you mean {hint[0]!r}?" if hint
                      else f" — use one of {', '.join(MODEL_ALIASES)} or a full claude-… id"))


def create_work_order(project_name: str, title: str, description: str = "",
                      origin: str = "jarvis", model: str | None = None,
                      effort: str | None = None, permission_mode: str | None = None,
                      append_system_prompt: str | None = None,
                      backlog_id: str | None = None,
                      depends_on: list[str] | None = None,
                      blocked_by: list[str] | None = None,
                      parent_id: str | None = None,
                      issue_url: str | None = None,
                      issue_priority: str | None = None,
                      budget_usd: float | None = None,
                      observability: str | None = None,
                      gap_class: str | None = None,
                      detector_id: str | None = None,
                      remedy_rule_id: str | None = None,
                      trigger_wo_id: str | None = None,
                      metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    """File a work order (docs/superpowers/specs/2026-09-27-self-evolution.md)"""
    from . import preconditions

    check_model_id(model)
    paths = registered_project_paths()
    if project_name not in paths:
        raise OpsError(f"project {project_name!r} not registered "
                       f"(known: {sorted(paths)}). Run `jarvis start` first.")
    try:
        holds = [preconditions.checked_ref(ref) for ref in (blocked_by or [])]
    except preconditions.PreconditionError as e:
        raise OpsError(f"cannot create this work order: {e}") from e
    # THE REFUSAL, before the store is even opened, so a refused order leaves no row
    backlog_hold: tuple[str, str] | None = None
    if backlog_id and not (depends_on or holds):
        central = CentralStore()
        try:
            item = central.get_backlog(backlog_id)
        finally:
            central.close()
        if item and item["status"] == "open":
            matched = preconditions.states_hold(item["title"], item["description"] or "")
            if matched:
                backlog_hold = (backlog_id, matched)
    stated = preconditions.states_hold(title, description)
    if stated and not (depends_on or holds or backlog_hold):
        raise OpsError(
            f"cannot create this work order: its brief states a stop condition — "
            f"\"{stated}\" — and nothing holds it back, so jarvisd would claim and "
            f"dispatch it (wo-314221b5 cost 0.93 USD this way). Wire the hold: "
            f"`--depends-on <wo-id>` when another work order in this project must "
            f"COMPLETE first, `--blocked-by deployed:<wo-id|sha>` when it needs that "
            f"work order's code RUNNING in production and not merely merged, or "
            f"`--blocked-by <bl-…|kn-…|io-…|#issue|text>` when it waits on something "
            f"that is not a work order. The brief may keep the sentence either way."
        )
    store = ProjectStore(paths[project_name])
    try:
        if parent_id:
            try:
                parent = store.get_feature_order(parent_id)
            except KeyError as e:
                raise OpsError(f"no feature order {parent_id!r} in {project_name!r} — "
                               f"a child is filed under a feature of its own project") from e
            if parent["status"] not in FO_OPEN_STATUSES:
                raise OpsError(
                    f"{parent_id} is {parent['status']}, so nothing more can be filed "
                    f"under it — file this work order on its own, or open a new feature"
                )
        wo = store.create_work_order(
            title=title, description=description, origin=origin, model=model,
            effort=effort, permission_mode=permission_mode,
            append_system_prompt=append_system_prompt, backlog_id=backlog_id,
            depends_on=depends_on, parent_id=parent_id, issue_url=issue_url,
            issue_priority=issue_priority,
            # AT CREATION, never a follow-up write: the daemon can claim and dispatch a new work order before a...
            metadata=metadata,
            budget_usd=(budget_usd if budget_usd is not None
                        else budget.default_for(_spec_or_none(project_name))),
            # Stamped, never resolved against the catalog here: NULL means "this order has no answer" and the... (docs/superpowers/specs/2026-09-24-order-observability.md §10.)
            observability=observability,
            gap_class=gap_class, detector_id=detector_id,
            remedy_rule_id=remedy_rule_id, trigger_wo_id=trigger_wo_id,
        )
        # AT CREATION with the store still open
        for ref in holds:
            store.add_precondition(wo["id"], ref)
        if backlog_hold:                                    # §2.4.1
            store.add_precondition(wo["id"], backlog_hold[0], note=backlog_hold[1])
        # AT CREATION, from the brief the order is actually given
        record_issue_references(store, paths[project_name], wo)
        return wo
    except (KeyError, ValueError) as e:
        # A dependency on a work order in another project cannot be honoured
        raise OpsError(f"cannot create the work order: {e} "
                       f"(dependencies are resolved within {project_name!r})") from e
    finally:
        store.close()


def blocked_by(store: ProjectStore, wo: dict[str, Any]) -> list[dict[str, Any]]:
    """Unfinished dependencies, without a query for the overwhelming majority"""
    if not store.dependencies(wo):
        return []
    return store.unfinished_dependencies(wo["id"])


def open_preconditions(store: ProjectStore, wo: dict[str, Any]) -> list[dict[str, Any]]:
    """The explicit holds still standing on this work order — §2.7"""
    return store.open_preconditions(wo["id"])


def fleet_if_held(wos: Iterable[dict[str, Any]]) -> "fleet.Fleet | None":
    """The account's state, for surfaces that render work orders an outage could hold (#714)"""
    if not any(wo["status"] in invariants.FLEET_HELD_STATUSES for wo in wos):
        return None
    try:
        return fleet.current(resolve_catalog())
    except (OpsError, CatalogError):
        return None


def find_work_order(wo_id: str, project_name: str | None = None
                    ) -> tuple[str, Path, dict[str, Any]]:
    """Locate a work order across all registered projects"""
    paths = registered_project_paths()
    # Guard before the lookup, not after: callers (the CLI
    if project_name and project_name not in paths:
        raise OpsError(f"project {project_name!r} not registered "
                       f"(known: {sorted(paths)})")
    candidates = {project_name: paths[project_name]} if project_name else paths
    for name, path in candidates.items():
        if not path.is_dir():
            continue
        store = ProjectStore(path)
        try:
            wo = store.get_work_order(wo_id)
            return name, path, wo
        except KeyError:
            continue
        finally:
            store.close()
    raise OpsError(f"work order {wo_id!r} not found in any registered project")


def _project_for_cwd(cwd: str, paths: dict[str, Path]) -> str | None:
    """Which registered project owns this directory, if any"""
    best: tuple[int, str] | None = None
    for name, path in paths.items():
        root = str(path)
        if cwd == root or cwd.startswith(root.rstrip("/") + "/"):
            if best is None or len(root) > best[0]:
                best = (len(root), name)
    return best[1] if best else None


def injectable_sessions(project_name: str, timeout: int = 5) -> dict[str, Any]:
    """The user's live Claude sessions under a project that Jarvis is not tracking"""
    from . import claude_cli

    paths = registered_project_paths()
    if project_name not in paths:
        return {"sessions": [], "error": f"project {project_name!r} is not registered"}
    if not claude_cli.available():
        return {"sessions": [], "error": "the `claude` CLI is not on PATH"}
    try:
        roster = claude_cli.list_background_sessions(timeout=timeout)
    except claude_cli.ClaudeCliError as e:
        return {"sessions": [], "error": f"could not list sessions: {e}"}

    store = ProjectStore(paths[project_name])
    try:
        known = {wo["session_id"] for wo in store.list_work_orders(include_hidden=True)
                 if wo.get("session_id")}
    finally:
        store.close()
    root = str(paths[project_name])
    return {"sessions": [
        {"session_id": s.session_id, "bg_id": s.id, "name": s.name, "state": s.state,
         "cwd": s.cwd, "started_at": s.started_at}
        for s in roster
        if s.session_id and s.session_id not in known and not s.is_finished
        and (s.cwd == root or s.cwd.startswith(root.rstrip("/") + "/"))
    ], "error": ""}


def inject_session(session_id: str, project_name: str | None = None,
                   title: str | None = None) -> dict[str, Any]:
    """Hand a Claude session the user started over to Jarvis, as a work order"""
    from . import claude_cli

    if not claude_cli.available():
        raise OpsError("the `claude` CLI is not on PATH, so its sessions cannot be read")
    try:
        roster = claude_cli.list_background_sessions()
    except claude_cli.ClaudeCliError as e:
        raise OpsError(f"could not list Claude sessions: {e}") from e

    # Accept either identifier: `claude agents --json` reports both a session id and its own agent id
    match = next((s for s in roster if s.session_id == session_id), None) \
        or next((s for s in roster if s.id == session_id), None)
    if match is None:
        raise OpsError(
            f"no Claude session {session_id!r} — `claude agents` lists the live ones"
        )
    if not match.session_id:
        raise OpsError(f"session {session_id!r} has no session id yet; try again once "
                       f"it has started")

    paths = registered_project_paths()
    if project_name:
        if project_name not in paths:
            raise OpsError(f"project {project_name!r} not registered "
                           f"(known: {sorted(paths)})")
        target = project_name
    else:
        target = _project_for_cwd(match.cwd, paths) or ""
        if not target:
            raise OpsError(
                f"session {session_id!r} runs in {match.cwd!r}, which is not inside any "
                f"registered project — pass --project to say where it belongs"
            )

    store = ProjectStore(paths[target])
    try:
        # Re-injecting is a no-op rather than an error: the point is that Jarvis knows about the session
        existing = store.find_by_session(match.session_id)
        if existing:
            note = f"already tracked as {existing['id']} ({existing['origin']})"
            # Re-injecting a session that was retired when it went idle picks tracking back up
            if (existing["origin"] == "injected" and existing["status"] == "completed"
                    and match.is_active):
                store.set_status(existing["id"], "running")
                store.add_event(existing["id"], "session_injected",
                                {"session_id": match.session_id, "state": match.state,
                                 "reopened": True})
                note = f"{existing['id']} was retired when the session went idle; "
                note += "it is running again, so tracking has resumed"
            return {"project": target, "wo_id": existing["id"],
                    "title": existing["title"],
                    "status": store.get_work_order(existing["id"])["status"],
                    "session_id": match.session_id, "already_known": True,
                    "note": note}
        # Mirror the session's current state, exactly as the daemon's tracker would
        status = ("waiting_input" if match.is_blocked
                  else "completed" if match.is_finished else "running")
        wo = store.create_work_order(
            title=title or match.name or f"session {match.id}",
            description=(
                "A Claude session the user started themselves and handed to Jarvis "
                "with `jarvis wo inject`. Jarvis did not dispatch it: it never received "
                "the worker briefing, so it owes no `jarvis wo finish` and its ending is "
                "not a failure."
            ),
            origin="injected",
            status=status,
            session_id=match.session_id,
        )
        store.add_event(wo["id"], "session_injected", {
            "session_id": match.session_id, "bg_id": match.id, "cwd": match.cwd,
            "state": match.state, "name": match.name,
        })
        if match.is_blocked:
            store.flag_attention(wo["id"],
                                 "session blocked (permission or input needed)")
        fresh = store.get_work_order(wo["id"])
        return {"project": target, "wo_id": fresh["id"], "title": fresh["title"],
                "status": fresh["status"], "session_id": match.session_id,
                "already_known": False,
                "note": "the session was not written to — `jarvis wo send "
                        f"{fresh['id']} \"…\"` is what starts driving it"}
    finally:
        store.close()


#: The environment variable `dispatch._write_worker_settings` stamps on every dispatched worker...
WORKER_SESSION_ENV = "JARVIS_WO_ID"


def user_authorship(relay: bool) -> str:
    """`MESSAGE_AUTHOR_USER` if this call can be attributed to the human, else `''` (docs/superpowers/specs/2026-09-11-a-gate-request-carries-the-users-words.md)"""
    from .project_store import MESSAGE_AUTHOR_USER

    if not relay or os.environ.get(WORKER_SESSION_ENV, "").strip():
        return ""
    return MESSAGE_AUTHOR_USER


def send_message(wo_id: str, content: str, source: str = "jarvis",
                 project_name: str | None = None, relay: bool = False) -> dict[str, Any]:
    name, path, wo = find_work_order(wo_id, project_name)
    if wo["status"] in ("completed", "failed", "cancelled"):
        # Still allowed — resuming a finished session is fine — but tell the user
        note = f"note: work order is {wo['status']}; the session will be revived"
        # §9.3 of docs/superpowers/specs/2026-09-30-a-failed-order-has-no-retry-path.md
        if wo["status"] == "failed":
            note += f" (`jarvis wo retry {wo_id}` is the named form of this)"
    else:
        note = None
    store = ProjectStore(path)
    try:
        authored_by = user_authorship(relay)
        msg_id = store.queue_message(wo_id, content, source=source,
                                     authored_by=authored_by)
        store.add_event(wo_id, "message_queued",
                        {"msg_id": msg_id, "source": source,
                         "authored_by": authored_by})
        # A reply IS the response to whatever flagged the user
        if wo["needs_attention"]:
            store.clear_attention(wo_id)
    finally:
        store.close()
    return {"project": name, "wo_id": wo_id, "msg_id": msg_id, "note": note,
            "delivery": ("delivered at the worker's next tool call if it is mid-turn, "
                         "otherwise as its next turn")}


#: What a message-less `jarvis wo retry` puts on the queue. §3 of... (docs/superpowers/specs/2026-09-30-a-failed-order-has-no-retry-path.md)
RETRY_NOTE = (
    "The OS is relaunching this work order because the user asked for it. Its last turn "
    "ended without a result, so the OS recorded it as failed — nothing about the work was "
    "judged wrong. Say where you got to, then carry on from there; do not start again. "
    "Finish with `jarvis wo finish` when the work is done."
)

#: §5: no status write, so the order really is still `failed` until the turn goes out
RETRY_QUEUED = ("queued as message {msg}; jarvisd launches the turn on its next tick. The "
                "order stays `failed` — and stays flagged — until that turn starts.")


def retry_refusal(wo: dict[str, Any]) -> str | None:
    """Why this work order CANNOT be retried, in the sentence that refuses it — or None (docs/superpowers/specs/2026-09-30-a-failed-order-has-no-retry-path.md)"""
    wo_id = str(wo["id"])
    status = str(wo["status"] or "")
    if status != "failed":
        return (f"{wo_id} is {status}, not failed — `wo retry` relaunches an order whose "
                f"worker died without delivering. Carry on a {status} one with "
                f"`jarvis wo send {wo_id} \"…\"`.")
    if not str(wo.get("session_id") or ""):
        return (f"{wo_id} failed before it ever opened a conversation, so there is no "
                f"session to relaunch — a message queued here would sit undelivered "
                f"(`worker_session.delivery_hold` holds it: \"it has no session to "
                f"resume\"). Nothing here can be retried; file the work again.")
    return None


def retry_queued_notice(msg_id: int) -> str:
    """`RETRY_QUEUED` for a page that has just redirected after a retry"""
    return RETRY_QUEUED.format(msg=msg_id)


def retry_state(store: ProjectStore, wo: dict[str, Any]) -> dict[str, Any] | None:
    """What the work-order page's retry control shows, or None (docs/superpowers/specs/2026-09-30-a-failed-order-has-no-retry-path.md)"""
    if str(wo["status"] or "") != "failed":
        return None
    refusal = retry_refusal(wo)
    return {"can_retry": refusal is None, "refusal": refusal,
            "assumptions": len(store.pending_assumptions(str(wo["id"])))}


def harvest_state(store: ProjectStore, wo: dict[str, Any]) -> dict[str, Any] | None:
    """What the OS read off disk when this order's latest turn died, or None (docs/superpowers/specs/2026-09-30-harvesting-a-dead-turn.md)"""
    payload = harvest.of_turn(store, wo)
    if not payload:
        return None
    authored = payload.get("authored") or {}
    dirty = list(authored.get("dirty") or ())
    commits = int(authored.get("commits") or 0)
    jobs = int(payload.get("jobs") or 0)
    if payload.get("empty"):
        line = "nothing — the worktree was clean and the turn said nothing"
    else:
        parts = []
        if commits:
            # PLAIN PROSE, no markdown: this one string is rendered by the HTML page as well as by the terminal
            parts.append(f"{commits} commit{'s' if commits != 1 else ''} on "
                         f"{authored.get('branch') or 'its branch'}")
        if dirty:
            parts.append(f"{len(dirty)} uncommitted file"
                         f"{'s' if len(dirty) != 1 else ''}"
                         + (f" checkpointed as {payload['checkpoint']}"
                            if payload.get("checkpoint") else " NOT checkpointed"
                            + (f" ({payload['checkpoint_skipped']})"
                               if payload.get("checkpoint_skipped") else "")))
        if payload.get("said"):
            parts.append("its last message")
        if jobs:
            parts.append(f"{jobs} orphaned background job"
                         f"{'s' if jobs != 1 else ''}")
        line = ", ".join(parts) or "nothing"
    return {"seq": payload.get("seq"), "empty": bool(payload.get("empty")),
            "line": line, "said": str(payload.get("said") or ""),
            "branch": str(authored.get("branch") or ""),
            "base": str(authored.get("base") or ""),
            "commits": commits, "dirty": dirty,
            "turn_commits": list(payload.get("turn_commits") or ()),
            "since": str(payload.get("since") or ""),
            "checkpoint": str(payload.get("checkpoint") or ""),
            "checkpoint_skipped": str(payload.get("checkpoint_skipped") or ""),
            "detached": bool(payload.get("detached")),
            "upstream": str(payload.get("upstream") or ""),
            "unpushed": int(payload.get("unpushed") or 0),
            "pr_url": str(payload.get("pr_url") or ""), "jobs": jobs,
            "unreadable": str(payload.get("unreadable") or "")}


def retry(wo_id: str, message: str | None = None, project_name: str | None = None,
          relay: bool = False, model: str | None = None) -> dict[str, Any]:
    """`jarvis wo retry` — relaunch a `failed` work order in its own session (docs/superpowers/specs/2026-09-30-a-failed-order-has-no-retry-path.md)"""
    check_model_id(model)
    name, path, wo = find_work_order(wo_id, project_name)
    refusal = retry_refusal(wo)
    if refusal is not None:
        raise OpsError(refusal)
    # §6 of docs/superpowers/specs/2026-09-30-harvesting-a-dead-turn.md: what the OS read off disk beats...
    brief_store = ProjectStore(path)
    try:
        if model:
            # Before the send: the relaunch reads `model` off the row
            brief_store.update_work_order(wo_id, model=model)
        text = (message if message is not None
                else (harvest.retry_brief(brief_store, wo) or RETRY_NOTE))
    finally:
        brief_store.close()
    # §3: an OS literal must never carry the user's stamp, whatever the surface said
    sent = send_message(wo_id, text, source="retry", project_name=name,
                        relay=relay and message is not None)
    store = ProjectStore(path)
    try:
        # After the delegated send, so a crash between them leaves a queued message and no claim about who...
        store.add_event(wo_id, "retry_requested",
                        {"msg_id": sent["msg_id"], "authored": bool(message)})
        pending = store.pending_assumptions(wo_id)
    finally:
        store.close()
    return {"project": name, "wo_id": wo_id, "msg_id": sent["msg_id"],
            "status": "failed", "authored": bool(message),
            "note": retry_queued_notice(sent["msg_id"]), "assumptions": len(pending)}


def waiting_on(store: ProjectStore, wo: dict[str, Any], *,
               now: float | None = None) -> dict[str, Any]:
    """What this work order is actually waiting for, and whether a nudge could help"""
    from . import worker_session
    from .invariants import awaiting_neo, neo_question_blocker
    from .neo_store import USER_HELD_Q_STATUSES

    wo_id = wo["id"]
    round_note = invariants.parallel_round_note(store, wo_id)
    pending = store.pending_assumptions(wo_id)
    if pending:
        return {"what": "assumptions", "stalled": False,
                "detail": f"{len(pending)} assumption(s) await your review — "
                          f"`jarvis wo review {wo_id}`{round_note}"}
    escalated = store.escalated_approvals(wo_id)
    if escalated:
        return {"what": "gate_escalated", "stalled": False,
                "detail": f"a gate Neo sent up to you — `jarvis gate approve "
                          f"{escalated[0]['id']} --reason \"…\"` (or `deny`)"}
    if store.pending_approvals(wo_id):
        return {"what": "gate_with_neo", "stalled": False,
                "detail": "a privileged-action gate that is with Neo — the verdict "
                          "reaches the worker by itself"}
    # Recorded, unargued, in front of NOBODY — `gates.AWAITING_CASE`
    held = store.held_approvals(wo_id)
    if held:
        # Both exits, addressed by REQUEST NUMBER: this describes what the worker must type
        return {"what": "gate_held", "stalled": False,
                "detail": f"gate {held[0]['id']} is recorded but unargued — no reviewer "
                          f"sees it yet, and the move is the WORKER's, either "
                          f"`jarvis gate request {held[0]['id']} --why \"…\" "
                          f"--evidence \"…\"` or, if the gate matched it by mistake, "
                          f"`jarvis gate contest {held[0]['id']} --why \"…\"`. If neither "
                          f"comes the OS abandons it unreviewed on the "
                          f"`gates.case_ttl_seconds` timer"}
    question = awaiting_neo(wo_id)
    # The worker's own escalated question on an order parked for review falls through to the status arm:... (docs/superpowers/specs/2026-09-28-stale-blockers-outlive-what-settled-them.md)
    outranked = (question is not None
                 and question["status"] in USER_HELD_Q_STATUSES
                 and question["kind"] == "question"
                 and wo["status"] == "needs_review")
    if question is not None and not outranked:
        if question["status"] in USER_HELD_Q_STATUSES:
            return {"what": "neo_escalated", "stalled": False,
                    "detail": neo_question_blocker(question)}
        return {"what": "neo_question", "stalled": False,
                "detail": f"Neo is answering question {question['id']} — the answer "
                          f"arrives as the worker's next turn by itself"}
    queued = store.queued_messages(wo_id)
    if queued:
        # ...unless the delivery this promises has stopped happening
        from .invariants import stuck_message

        found = stuck_message(store, wo, now=now)
        if found is not None:
            msg, why = found
            return {"what": "message_stuck", "stalled": False,
                    "detail": f"message {msg['id']} is queued undelivered because "
                              f"{why} — read `jarvis wo show {wo_id}`, then `jarvis wo "
                              f"done` if the work order is finished with it"}
        return {"what": "queued_message", "stalled": False,
                "detail": f"{len(queued)} message(s) queued — jarvisd delivers them "
                          f"when the worker is idle"}
    # Parked on the user's Claude Code sign-in (`Daemon._park_on_signin`)
    pause = worker_session.turn_pause(store, wo_id)
    if pause is not None and pause.reason == worker_session.PAUSE_AUTH:
        return {"what": "signin", "stalled": False,
                "detail": "Claude Code could not authenticate — run `/login`, and the "
                          "OS resumes this and every other parked order by itself"}
    # ...and the two pauses that DO name a moment
    if pause is not None and pause.resumable:
        return {"what": "retry_pending", "stalled": False,
                "detail": f"the turn stopped on a "
                          f"{worker_session.PAUSE_NOUN[pause.reason]} error and the OS "
                          f"relaunches it by itself"}
    # A live turn means "working" for every status EXCEPT `waiting_input`
    turn = store.latest_turn(wo_id)
    if (turn is not None and turn["state"] == "running"
            and wo["status"] != "waiting_input"):
        return {"what": "turn_running", "stalled": False,
                "detail": "a turn is in flight — the worker is working"}
    # Before the statuses below, because since issue 212 a round runs in PARALLEL with a `needs_review`...
    if round_note:
        return {"what": "validating", "stalled": False,
                "detail": f"the validation panel is judging it — the verdict settles "
                          f"the work order by itself{round_note}"}
    # ABOVE both catch-alls below, which answer "not dispatched yet
    rows = open_preconditions(store, wo)
    if rows:
        refs = ", ".join(row["ref"] for row in rows)
        return {"what": "precondition", "stalled": False,
                "detail": f"it was told not to start until {refs} clears — "
                          f"`jarvis wo unblock {wo_id}` cuts the hold"}
    hold = store.plan_hold(wo)
    if hold:
        return {"what": "plan_assumptions", "stalled": False,
                "detail": f"its feature's plan is waiting on you — {hold['n']} "
                          f"assumption(s) on {hold['planner_id']}; "
                          f"`jarvis wo review {hold['planner_id']}`"}
    # §6 of docs/superpowers/specs/2026-09-30-a-failed-order-has-no-retry-path.md
    if wo["status"] == "failed":
        return {"what": "failed", "stalled": False,
                "detail": f"the worker died without delivering — `jarvis wo retry {wo_id}` "
                          f"relaunches it in the same session; nothing is running to nudge"}
    if wo["status"] in ("completed", "cancelled", "waiting_pr_merge",
                        "needs_review"):
        return {"what": wo["status"], "stalled": False,
                "detail": f"the work order is {wo['status']} — nothing is running to "
                          f"nudge"}
    if wo["status"] == "pending":
        return {"what": "pending", "stalled": False,
                "detail": "not dispatched yet — no worker exists to nudge"}
    # THE FALL-THROUGH BELOW USED TO ANSWER FOR THIS ONE (#264)
    if wo["status"] == "idle":
        return {"what": "manager_idle", "stalled": False,
                "detail": "it is the feature's manager and has nothing to act on — "
                          "idle until its feature sends it something, which is what "
                          "this work order is for"}
    return {"what": "prompt", "stalled": True,
            "detail": "nothing else accounts for it: an unanswered permission prompt "
                      "is what is left"}


#: The `waiting_on` answers where a nudge is ACTIVELY WRONG rather than merely useless (#264)
NUDGE_IS_WRONG = {
    "message_stuck": "a nudge cannot help — it is another message on the queue that is "
                     "already stuck.",
    "manager_idle": "a nudge cannot help — it buys one turn of the manager saying "
                    "nothing was needed, and it ends up back here.",
}


def resume_in_auto(wo_id: str, project_name: str | None = None,
                   force: bool = False) -> dict[str, Any]:
    """Diagnose what a work order is waiting on, and unstick it only if a nudge can"""
    name, path, wo = find_work_order(wo_id, project_name)
    store = ProjectStore(path)
    try:
        wait = waiting_on(store, wo)
        mode = wo["permission_mode"] or _project_permission_mode(name)
    finally:
        store.close()
    could_prompt = worker_stalls_on_prompts(mode) if mode else True
    out = {"project": name, "wo_id": wo_id, "permission_mode": mode,
           "waiting_on": wait["what"], "diagnosis": wait["detail"]}
    if not force and wait["what"] in NUDGE_IS_WRONG:
        store = ProjectStore(path)
        try:
            store.add_event(wo_id, "resume_auto_declined",
                            {"permission_mode": mode, "waiting_on": wait["what"]})
        finally:
            store.close()
        out.update({
            "nudged": False, "changed": False,
            "note": f"{NUDGE_IS_WRONG[wait['what']]} {wait['detail']}. Send one anyway "
                    f"with --force.",
        })
        return out
    if not force and not could_prompt and not wait["stalled"]:
        # The no-op case, reported rather than performed
        store = ProjectStore(path)
        try:
            store.add_event(wo_id, "resume_auto_declined",
                            {"permission_mode": mode, "waiting_on": wait["what"]})
        finally:
            store.close()
        out.update({
            "nudged": False, "changed": False,
            "note": f"nothing to unstick — workers here already run in {mode!r}, which "
                    f"never prompts, so there is no permission prompt to clear. "
                    f"{wait['detail']}. Nudge it anyway with --force.",
        })
        return out
    previous = wo["permission_mode"]
    store = ProjectStore(path)
    try:
        if previous != "auto":
            store.update_work_order(wo_id, permission_mode="auto")
            store.add_event(wo_id, "permission_mode_changed",
                            {"from": previous, "to": "auto", "by": "resume_in_auto"})
    finally:
        store.close()
    send_message(
        wo_id,
        "Your permission mode is now `auto` — routine tools (reads, edits, tests, "
        "git) run without asking. Please continue the work order.",
        source="jarvis", project_name=name,
    )
    out.update({
        "nudged": True, "changed": previous != "auto", "permission_mode": "auto",
        "note": ("flipped to auto and nudged; jarvisd resumes the worker when idle"
                 if previous != "auto" else
                 f"mode was already 'auto' — nothing to flip; nudged anyway "
                 f"({'--force' if force else wait['detail']})"),
    })
    return out


def _project_permission_mode(project_name: str) -> str | None:
    """The mode this project's workers run in, from the catalog — None if unreadable"""
    try:
        catalog = resolve_catalog()
    except (OpsError, CatalogError):
        return None
    for spec in catalog.projects:
        if spec.name == project_name:
            return spec.worker.permission_mode
    return None


#: The `waiting_on` answers that mean THE USER IS NEEDED (#197)
USER_IS_NEEDED_WAITS = frozenset({
    "assumptions", "gate_escalated", "neo_escalated", "plan_assumptions",
    "message_stuck", "signin", "needs_review",
})

#: As much timeline as any conversation the fleet has run
DIAGNOSE_EVENT_LIMIT = 10_000


def ago_phrase(seconds: float) -> str:
    """"3.1h" — an age at the magnitude a reader can hold in their head"""
    if seconds < 90:
        return f"{int(seconds)}s"
    if seconds < 5400:
        return f"{int(seconds / 60)}m"
    if seconds < 129_600:
        return f"{seconds / 3600:.1f}h"
    return f"{seconds / 86400:.1f}d"


def _ago(ts: Any, now: float) -> dict[str, Any]:
    """`{ago_seconds, ago}` for one moment, for the clock block below"""
    seconds = max(0.0, now - float(ts))
    return {"ago_seconds": round(seconds, 1), "ago": ago_phrase(seconds)}


#: Said on every clock block, whatever it contains
LIVE_NOTE = ("this is the OS's own record of the order, not the process — `jarvis watch "
             "{wo_id}` is what reads the turn in flight")

#: `None` and a sentence, never `0` — issue #227
NO_TURN_NOTE = ("no worker turn has ever run for this order, so there is no last turn to "
                "date — absent, not zero")
NO_STATUS_NOTE = ("no status transition is on the timeline for this order, so there is "
                  "none to date — absent, not zero")
NO_EVENT_NOTE = ("this order has no timeline events at all, so there is nothing to date "
                 "— absent, not zero")

#: The residual is NOT a cause and is labelled as one thing only: idle the record does not account for
UNEXPLAINED_NOTE = ("idle the record does not account for — not a cause, a residual; a "
                    "large one is a different defect from anything this report can name")
NO_TRANSCRIPT_NOTE = ("the transcript for this order is absent (never written, or pruned "
                      "by Claude Code), so the residual is unmeasurable rather than zero")

# -- time in each state

#: How near a timeline event has to sit to a transition to be read as its cause
TRIGGER_WINDOW = 120.0

#: `None` and a sentence, never `0` — issue #227's rule, as `NO_TURN_NOTE` above
NO_ACTIVITY_NOTE = ("nothing has happened to this order on the record, so there is no "
                    "last activity to date — absent, not zero")
SETTLED_NOTE = "this order has settled, so it has no current status age"
#: Said when the open span and the order's `status` column disagree: the column is the status (docs/superpowers/specs/2026-09-28-stale-blockers-outlive-what-settled-them.md)
SPANS_BEHIND_NOTE = "the recorded spans do not reach this order's current status"
FO_APPROXIMATE_NOTE = ("approximate — a feature order keeps no event trail, so this is one "
                       "coarse span from its creation")
#: What a renderer prints for a transition nothing was written about
NO_TRIGGER_PHRASE = "cause not recorded"

#: What is NOT activity
def _quiet_kinds() -> tuple[str, ...]:
    return tuple(sorted({*health.observer_kinds(), "status", "created"}))


@dataclass(frozen=True)
class Span:
    """One status the order was in, and when"""

    status: str
    entered: float
    left: float | None
    trigger: str                # '' when the record names no cause
    approximate: bool

    def seconds(self, now: float) -> float:
        """A backwards clock must not produce a negative span (`holds.Hold.overlap`)"""
        return max(0.0, (now if self.left is None else self.left) - self.entered)


@dataclass(frozen=True)
class StateDurations:
    """One order's whole status history"""

    order_id: str
    order_kind: str             # 'wo' | 'fo'
    spans: tuple[Span, ...]     # oldest first
    current_status: str         # '' for a settled order
    current_status_since: float | None
    last_activity_ts: float | None
    last_activity_kind: str     # the wo_events kind / table that supplied it, '' if none
    approximate: bool           # any span is
    notes: tuple[str, ...]
    #: FO_KINDS, so an improvement order's rows read `analysing` and not `planning`
    row_kind: str = ""
    #: The read moment `state_durations` was given
    asof: float = 0.0
    #: Every interval `holds` says the order was not permitted to run (docs/superpowers/specs/2026-09-30-time-in-state-counts-a-usage-limit-hold-as-running.md)
    holds: tuple[Hold, ...] = ()

    def label(self, status: str) -> str:
        if self.order_kind == "fo":
            return feature_status_label(self.row_kind, status)
        return timeline.STATUS_LABEL.get(status, status)

    def as_dict(self, now: float | None = None) -> dict[str, Any]:
        now = self.asof if now is None else now
        order = FO_STATUSES if self.order_kind == "fo" else WO_STATUSES
        end = now
        if self.spans and self.spans[-1].left is not None:
            end = self.spans[-1].left
        lifetime = max(0.0, end - self.spans[0].entered) if self.spans else 0.0

        # Spec §1: the subtraction is the READER's, per span, off the holds this reading already carries
        from . import holds as holds_mod

        def held_in(start: float, end: float) -> float:
            return sum(h.overlap(start, end, now) for h in self.holds)

        def held_of(span: Span) -> float:
            return held_in(span.entered, now if span.left is None else span.left)

        total_held = held_in(self.spans[0].entered, end) if self.spans else 0.0
        lifetime_active = max(0.0, lifetime - total_held)

        def share(seconds: float) -> float:
            return round(seconds / lifetime, 4) if lifetime else 0.0

        # Spec §4: `share` stays wall over wall, because the Gantt's offsets are wall positions
        def active_share(seconds: float) -> float:
            return round(seconds / lifetime_active, 4) if lifetime_active else 0.0

        def by_cause(windows: Sequence[tuple[float, float]]) -> list[dict[str, Any]]:
            """Biggest cause first — summed per window"""
            totals: dict[str, float] = {}
            for start, stop in windows:
                for cause, seconds in holds_mod.by_cause(self.holds, start, stop,
                                                         now=now).items():
                    totals[cause] = totals.get(cause, 0.0) + seconds
            return [{"cause": cause, "phrase": holds_mod.HOLD_CAUSES.get(cause, cause),
                     "seconds": round(seconds, 2), "seconds_human": ago_phrase(seconds)}
                    for cause, seconds in sorted(totals.items(), key=lambda kv: -kv[1])]

        def basis(seconds: float, held: float) -> dict[str, Any]:
            active = max(0.0, seconds - held)
            return {"held_seconds": round(held, 2), "held_human": ago_phrase(held),
                    "active_seconds": round(active, 2),
                    "active_human": ago_phrase(active),
                    "active_share": active_share(active)}

        spans = [{"status": s.status, "label": self.label(s.status), "entered": s.entered,
                  "left": s.left, "open": s.left is None,
                  "seconds": round(s.seconds(now), 2),
                  "seconds_human": ago_phrase(s.seconds(now)),
                  "share": share(s.seconds(now)), "trigger": s.trigger,
                  "approximate": s.approximate, **basis(s.seconds(now), held_of(s))}
                 for s in self.spans]
        totals = []
        for status in order:
            mine = [s for s in self.spans if s.status == status]
            if not mine:
                continue
            seconds = round(sum(s.seconds(now) for s in mine), 2)
            # Summed per span and never over a synthetic window: a status entered twice must not claim a hold...
            held = sum(held_of(s) for s in mine)
            totals.append({"status": status, "label": self.label(status),
                           "seconds": seconds, "seconds_human": ago_phrase(seconds),
                           "entries": len(mine), "share": share(seconds),
                           **basis(seconds, held),
                           "held_by": by_cause([(s.entered,
                                                 now if s.left is None else s.left)
                                                for s in mine])})
        age = (round(now - self.current_status_since, 2)
               if self.current_status_since is not None else None)
        idle = (round(max(0.0, now - self.last_activity_ts), 2)
                if self.last_activity_ts is not None else None)
        # The current status' own hold, and the open one if there is one: `None` and never a zero-filled... (#227)
        since = self.current_status_since
        status_held = round(held_in(since, now), 2) if since is not None else None
        active_age = (None if age is None or status_held is None
                      else round(max(0.0, age - status_held), 2))
        open_hold = ([h for h in self.holds if h.open] or [None])[-1]
        return {
            "order_id": self.order_id, "order_kind": self.order_kind, "now": now,
            "approximate": self.approximate,
            "lifetime_seconds": round(lifetime, 2),
            "lifetime_human": ago_phrase(lifetime),
            "spans": spans, "totals": totals,
            "current_status": self.current_status,
            "current_status_since": self.current_status_since,
            "current_status_age": age,
            "current_status_age_human": None if age is None else ago_phrase(age),
            "last_activity_ts": self.last_activity_ts,
            "last_activity_kind": self.last_activity_kind,
            "last_activity_age": idle,
            "last_activity_age_human": None if idle is None else ago_phrase(idle),
            "notes": list(self.notes),
            "lifetime_held_seconds": round(total_held, 2),
            "lifetime_active_seconds": round(lifetime_active, 2),
            "lifetime_active_human": ago_phrase(lifetime_active),
            "current_status_held_seconds": status_held,
            "current_status_active_age": active_age,
            "current_status_active_age_human": (None if active_age is None
                                                else ago_phrase(active_age)),
            "held_now": (None if open_hold is None else {
                "cause": open_hold.cause, "phrase": open_hold.phrase,
                "since": open_hold.started,
                "seconds": round(open_hold.finish(now) - open_hold.started, 2),
                "seconds_human": ago_phrase(open_hold.finish(now)
                                            - open_hold.started)}),
            "current_status_held_by": ([] if since is None
                                       else by_cause([(since, now)])),
        }


def _derived_trigger(store: ProjectStore, wo_id: str, entered: float) -> str:
    """The kind of the newest quiet-excluded event at `ts <= entered` within the window"""
    quiet = _quiet_kinds()
    marks = ", ".join("?" for _ in quiet)
    row = store.conn.execute(
        f"SELECT kind FROM wo_events WHERE wo_id=? AND ts<=? AND ts>=? "
        f"AND kind NOT IN ({marks}) ORDER BY ts DESC, id DESC LIMIT 1",
        (wo_id, entered, entered - TRIGGER_WINDOW, *quiet),
    ).fetchone()
    return str(row["kind"]) if row else ""


def _transcript_activity(store: ProjectStore, wo_id: str) -> list[tuple[float, str]]:
    """The last write to the worker's transcript and to the subagent files beside it (docs/superpowers/specs/2026-09-29-a-heredoc-edit-is-not-a-merge.md)"""
    from . import claude_cli, inspection
    try:
        wo = store.get_work_order(wo_id)
        session_id = str(wo.get("session_id") or "")
        if not session_id:
            return []
        # `worker_session.worktree_path`'s own computation
        worktree = wo.get("worktree")
        cwd = (store.project_path / ".claude" / "worktrees" / str(worktree)
               if worktree else store.project_path)
        path = claude_cli.session_transcript_path(cwd, session_id)
        stamps = []
        for p in [path, *inspection._subagent_transcripts(path)]:
            # `stat()` and not `exists()`: `exists()` swallows a PermissionError into False
            try:
                stamps.append(p.stat().st_mtime)
            except FileNotFoundError:
                continue
    except (OSError, KeyError):
        return []
    return [(max(stamps), "transcript")] if stamps else []


def _activity_of(store: ProjectStore, wo_id: str) -> list[tuple[float, str]]:
    """Every moment the record says something happened TO one work order"""
    quiet = _quiet_kinds()
    marks = ", ".join("?" for _ in quiet)
    found: list[tuple[float, str]] = []
    row = store.conn.execute(
        f"SELECT kind, ts FROM wo_events WHERE wo_id=? AND kind NOT IN ({marks}) "
        f"ORDER BY ts DESC, id DESC LIMIT 1", (wo_id, *quiet)).fetchone()
    if row is not None:
        found.append((float(row["ts"]), str(row["kind"])))
    for sql, source in (
        ("SELECT MAX(started_at), MAX(ended_at) FROM wo_turns WHERE wo_id=?", "wo_turns"),
        ("SELECT MAX(ts), MAX(delivered_at) FROM wo_messages WHERE wo_id=?",
         "wo_messages"),
        ("SELECT MAX(ts) FROM validation_rounds WHERE wo_id=?", "validation_rounds"),
        ("SELECT MAX(ts), MAX(decided_at) FROM approvals WHERE wo_id=?", "approvals"),
    ):
        got = store.conn.execute(sql, (wo_id,)).fetchone()
        stamps = [float(v) for v in tuple(got or ()) if v is not None]
        if stamps:
            found.append((max(stamps), source))
    found.extend(_transcript_activity(store, wo_id))
    return found


def _family_of(store: ProjectStore, fo_id: str) -> list[str]:
    """Every work order a feature is made of: its children, its planner, its manager (docs/superpowers/specs/2026-09-30-time-in-state-counts-a-usage-limit-hold-as-running.md)"""
    family = [c["id"] for c in store.feature_children(fo_id, kinds=WORKER_CHILDREN)]
    fo = store.get_feature_order(fo_id)
    if fo.get("plan_wo_id"):
        family.append(str(fo["plan_wo_id"]))
    manager = store.manager_work_order(fo_id)
    if manager:
        family.append(str(manager["id"]))
    return family


def _last_activity(store: ProjectStore, kind: str, order_id: str) -> tuple[float, str]:
    """`(ts, source)` for the newest thing that happened, `(0.0, '')` when nothing did"""
    if kind == "wo":
        found = _activity_of(store, order_id)
    else:
        found = [seen for child in _family_of(store, order_id)
                 for seen in _activity_of(store, child)]
        row = store.conn.execute(
            "SELECT MAX(ts) FROM validation_rounds WHERE fo_id=?", (order_id,)).fetchone()
        if row and row[0] is not None:
            found.append((float(row[0]), "validation_rounds"))
    if not found:
        return (0.0, "")
    return max(found, key=lambda seen: seen[0])


def state_durations(store: ProjectStore, *, wo_id: str = "", fo_id: str = "",
                    now: float | None = None) -> StateDurations:
    """How long this order has been in its status, and how long it spent in each before"""
    if bool(wo_id) == bool(fo_id):
        raise OpsError("state_durations takes exactly one of wo_id= or fo_id=")
    from . import db, holds as holds_mod

    now = db.now() if now is None else now
    kind = "wo" if wo_id else "fo"
    order_id = wo_id or fo_id
    # THE READER SUBTRACTS, not its five callers: an optional `spans=` would reproduce issue 887 for...
    episodes = (holds_mod.held(store, order_id, now=now) if kind == "wo"
                else holds_mod.held_family(store, _family_of(store, order_id), now=now))
    terminal = TERMINAL_STATUSES if kind == "wo" else FO_TERMINAL_STATUSES
    row_kind = ""
    # The order's own row, for `wo` too now: its `status` column is the status (docs/superpowers/specs/2026-09-28-stale-blockers-outlive-what-settled-them.md)
    row = (store.get_feature_order(order_id) if kind == "fo"
           else store.get_work_order(order_id))
    if kind == "fo":
        row_kind = str(row.get("kind") or "feature")
    column_status = str(row.get("status") or "")

    rows = store.state_spans(order_id)
    spans: list[Span] = []
    for i, row in enumerate(rows):
        entered = float(row["ts"])
        status = str(row["to_status"])
        if i + 1 < len(rows):
            left: float | None = float(rows[i + 1]["ts"])
        else:
            # The last span is OPEN unless the order settled there
            left = entered if status in terminal else None
        trigger = str(row["trigger"] or "")
        if not trigger and kind == "wo":
            trigger = _derived_trigger(store, order_id, entered)
        spans.append(Span(status=status, entered=entered, left=left, trigger=trigger,
                          approximate=bool(row["approximate"])))

    open_span = spans[-1] if spans and spans[-1].left is None else None
    # A span that disagrees with the column is not a present-tense claim: report the column
    behind = (open_span is not None and column_status
              and open_span.status != column_status)
    activity_ts, activity_kind = _last_activity(store, kind, order_id)
    approximate = any(s.approximate for s in spans)
    notes: list[str] = []
    if approximate:
        notes.append(FO_APPROXIMATE_NOTE)
    if not activity_kind:
        notes.append(NO_ACTIVITY_NOTE)
    if spans and open_span is None:
        notes.append(SETTLED_NOTE)
    if behind:
        notes.append(SPANS_BEHIND_NOTE)
    return StateDurations(
        order_id=order_id, order_kind=kind, spans=tuple(spans),
        current_status=(column_status if behind
                        else (open_span.status if open_span else "")),
        current_status_since=(open_span.entered
                              if open_span is not None and not behind else None),
        last_activity_ts=activity_ts if activity_kind else None,
        last_activity_kind=activity_kind, approximate=approximate,
        notes=tuple(notes), row_kind=row_kind, asof=now, holds=tuple(episodes),
    )

#: Pinned rule `kn-40db1828`, applied with full force (spec §6.4)
OS_CALLS_NOTE = ("`unreachable` means the OS could not reach the model on that call: it "
                 "produced no judgement at all, and nothing may be read into it. Only an "
                 "`answered` row carries anything a model actually said.")

#: Said whenever the read hit its cap, because a list cut off silently is read as the whole of it
OS_CALLS_CAPPED_NOTE = ("This list is capped at the newest {limit} calls, so the counts "
                        "beside it are a floor rather than the order's whole history.")

#: How many call rows the newest-first list is worth printing to a terminal
OS_CALLS_LIMIT = 200
OS_CALLS_SHOWN = 10

#: The clearing command lives INSIDE `waiting_on`'s sentence
WAY_THROUGH_NOTE = ("the blocker's own sentence above names the way through, in the "
                    "words of the function that diagnosed it")


def _diagnose_clock(store: ProjectStore, wo_id: str, now: float) -> dict[str, Any]:
    """The last state transition, the last turn, the last event — from the store alone"""
    statuses = store.events_of_kind(wo_id, "status")
    last_status = ({"status": (db.from_json(statuses[-1].get("payload"), {}) or {}
                               ).get("status") or "",
                    "ts": statuses[-1]["ts"], **_ago(statuses[-1]["ts"], now)}
                   if statuses else None)
    turn = store.latest_turn(wo_id)
    last_turn = ({"seq": turn["seq"], "label": turn_label(turn["seq"]),
                  "state": turn["state"],
                  "started_at": turn["started_at"], "ended_at": turn["ended_at"],
                  **_ago(turn["ended_at"] or turn["started_at"], now)}
                 if turn else None)
    events = store.list_events(wo_id, limit=DIAGNOSE_EVENT_LIMIT)
    last_event = ({"kind": events[-1]["kind"], "ts": events[-1]["ts"],
                   **_ago(events[-1]["ts"], now)} if events else None)
    notes = [note for note, absent in ((NO_STATUS_NOTE, last_status is None),
                                       (NO_TURN_NOTE, last_turn is None),
                                       (NO_EVENT_NOTE, last_event is None)) if absent]
    return {"last_status": last_status, "last_turn": last_turn,
            "last_event": last_event,
            "turn_open": bool(turn is not None and turn["state"] == "running"),
            "note": " ".join(notes), "live_note": LIVE_NOTE.format(wo_id=wo_id)}


def _diagnose_holds(store: ProjectStore, wo: dict[str, Any],
                    project: str, now: float) -> dict[str, Any]:
    """Every episode the record says held this order, plus the honest residual"""
    from . import autopsy
    from . import holds as holds_mod

    episodes = holds_mod.held(store, wo["id"], now=now)
    by_cause = {cause: round(seconds, 2) for cause, seconds
                in holds_mod.by_cause(episodes, float("-inf"), now, now=now).items()}
    # Exactly `inspect_report`'s inner `unit()` shape
    anatomy, provenance = autopsy.anatomy_for(
        wo, inspect_config(project), spans=list(episodes),
        turn_starts=store.turn_starts(wo["id"]))
    if anatomy.found:
        unexplained = {"seconds": round(anatomy.unexplained, 2),
                       "seconds_human": ago_phrase(anatomy.unexplained),
                       "residual": True, "note": UNEXPLAINED_NOTE}
    else:
        unexplained = {"seconds": None, "seconds_human": None, "residual": True,
                       "note": NO_TRANSCRIPT_NOTE}
    # `as_dict` verbatim plus the FORMATTED duration
    rows = [{**h.as_dict(now), "seconds_human": ago_phrase(h.as_dict(now)["seconds"]),
             # What held it, so a surface can link there — `None` when the record names nothing
             "key": h.key}
            for h in episodes]
    return {"episodes": rows, "by_cause": by_cause,
            "open": next((r for r in reversed(rows) if r["open"]), None),
            # Beside the one figure it attributes: which reading the residual came from
            "unexplained": unexplained, "provenance": provenance}


def _diagnose_os_calls(wo_id: str, limit: int = OS_CALLS_LIMIT,
                       shown: int = OS_CALLS_SHOWN) -> dict[str, Any]:
    """The OS's own `claude -p` calls for this order, each with its outcome (#227)"""
    rows = [{**row, "outcome": "answered" if row["ok"] else "unreachable"}
            for row in _os_calls_detail(wo_id, limit=limit)]
    note = OS_CALLS_NOTE
    capped = len(rows) >= limit
    if capped:
        note = f"{note} {OS_CALLS_CAPPED_NOTE.format(limit=limit)}"
    return {"calls": len(rows), "failed": sum(1 for r in rows if not r["ok"]),
            "rows": rows, "note": note, "limit": limit, "capped": capped,
            "shown_limit": shown, "more": max(0, len(rows) - shown)}


def ack_decision_blocker(blockers: list[str]) -> str | None:
    """The blocker that makes acking unacceptable, or None — the one extraction of it"""
    return next((b for b in blockers if "assumption" in b.lower()), None)


def ack_refusal(wo: dict[str, Any], blockers: list[str]) -> str | None:
    """Why `jarvis wo ack` would be refused on this order, or None if it would be taken"""
    blocker = ack_decision_blocker(blockers)
    if blocker is None:
        return None
    return (f"{wo['id']} is waiting on a decision ({blocker}) — acknowledging would "
            f"bury it. Use `jarvis wo review {wo['id']}` to accept, or `--reject` to "
            f"send it back.")


def _diagnose_commands(store: ProjectStore, wo: dict[str, Any], *, project: str,
                       blocker: dict[str, Any], needs_you: list[str],
                       ) -> tuple[list[dict[str, str]], list[str]]:
    """Only what the OS would accept RIGHT NOW, plus why the rest is missing"""
    wo_id = str(wo["id"])
    out: list[dict[str, str]] = []
    refusals: list[str] = []

    # FIRST — §7 of docs/superpowers/specs/2026-09-30-a-failed-order-has-no-retry-path.md
    retry_no = retry_refusal(wo)
    if retry_no is None:
        out.append({"command": f"jarvis wo retry {wo_id}",
                    "why": "relaunch the worker in its own session from where it died — "
                           "nothing about the work was judged wrong"})
    elif wo["status"] == "failed":
        refusals.append(retry_no)

    refusal = force_validation_refusal(store, wo, project=project,
                                       cfg=validation_config(project))
    if refusal is None:
        out.append({"command": f'jarvis validation force {wo_id} --reason "…"',
                    "why": "judge the current pull request again, now — no worker runs "
                           "and no round is open"})
    else:
        refusals.append(refusal)

    # `ack_attention`'s own predicate, called rather than copied (`ack_refusal`)
    if wo["needs_attention"]:
        ack_no = ack_refusal(wo, needs_you)
        if ack_no is None:
            out.append({"command": f"jarvis wo ack {wo_id}",
                        "why": "put the attention flag down for good — nothing here is a "
                               "decision that would be buried by it"})
        else:
            refusals.append(ack_no)

    blockers = store.unfinished_dependencies(wo_id)
    if blockers:
        out.append({"command": f"jarvis wo unblock {wo_id}",
                    "why": f"cut the dependency edges holding it back "
                           f"({', '.join(d['id'] for d in blockers)})"})
        if not invariants.dead_dependencies(store, wo):
            # `--all` is the only form `unblock_work_order` would accept here: with no dead edge the default cuts...
            out[-1] = {"command": f"jarvis wo unblock {wo_id} --all",
                       "why": "every edge it waits on is still LIVE, so only --all cuts "
                              "them — it then runs without the work it was to build on"}

    # NUDGE_IS_WRONG half mirrors `resume_in_auto`'s own refusal
    if blocker["stalled"] and blocker["what"] not in NUDGE_IS_WRONG:
        out.append({"command": f"jarvis wo resume-auto {wo_id}",
                    "why": "nothing is coming for this by itself, and this is the one "
                           "blocker a nudge can move"})

    for approval in store.escalated_approvals(wo_id):
        out.append({"command": f'jarvis gate approve {approval["id"]} --reason "…"',
                    "why": "a privileged action Neo sent up to you — nobody else can "
                           "open this gate"})
        out.append({"command": f'jarvis gate deny {approval["id"]} --reason "…"',
                    "why": "refuse it; the reason reaches the worker"})
    return out, refusals


# Metered at the DEFINITION, which is why §7's dashboard routes need no edit: they call this same... (docs/superpowers/specs/2026-09-24-order-observability.md)
@observability.metered(OBSERVE_WHY, target="wo_id", project="project_name")
def diagnose(wo_id: str, project_name: str | None = None) -> dict[str, Any]:
    """Why is this order not moving, and what do I type — `jarvis wo why` (docs/superpowers/specs/2026-09-24-order-observability.md)"""
    name, path, wo = find_work_order(wo_id, project_name)
    now = time.time()
    store = ProjectStore(path)
    try:
        # One read for one row, the way `os_status` takes one for a whole listing: the account's state can... (#714)
        held_by_fleet = fleet_if_held([wo])
        blocker = waiting_on(store, wo, now=now)
        needs_you = invariants.true_blockers(store, wo, now)
        # §8 of docs/superpowers/specs/2026-10-09-derive-somebody-is-going-to-act-on-this.md
        actor = (scheduled_actor(store, wo, now)
                 if wo["status"] in SCHEDULED_ACTORS else None)
        payload = {
            "wo_id": str(wo["id"]), "project": name, "title": wo["title"],
            "status": wo["status"],
            "status_label": invariants.status_label(store, wo, held_by_fleet),
            "blocker": blocker,
            "actor": ({"pass_name": actor.pass_name, "why": actor.why,
                       "predicate": actor.predicate} if actor is not None else None),
            "needs_you": needs_you,
            # All three `or None`: these return "" for "no note", and an empty string renders as a blank note (#227)
            "notes": {
                "parked": invariants.parked_reason(store, wo, now) or None,
                "pause": invariants.pause_note(store, wo) or None,
                "fleet_hold": invariants.fleet_hold_note(wo, held_by_fleet) or None,
            },
            "disagreements": _disagreements(blocker, needs_you),
            "clock": _diagnose_clock(store, str(wo["id"]), now),
            "holds": _diagnose_holds(store, wo, name, now),
            "way_through": {"detail": blocker["detail"], "note": WAY_THROUGH_NOTE},
        }
        commands, refusals = _diagnose_commands(store, wo, project=name,
                                                blocker=blocker, needs_you=needs_you)
    finally:
        store.close()
    # After the store is closed on purpose: this one reads the CENTRAL database and opens its own handle
    payload["os_calls"] = _diagnose_os_calls(str(wo["id"]))
    payload["commands"] = commands
    payload["refusals"] = refusals
    return payload


def _disagreements(blocker: dict[str, Any], needs_you: list[str]) -> list[str]:
    """Both answers, both sources, no winner — spec §6.1"""
    what = blocker["what"]
    out: list[str] = []
    if needs_you and what not in USER_IS_NEEDED_WAITS:
        out.append(
            f"these two disagree and neither is overruled here: `invariants."
            f"true_blockers` says this needs YOU ({needs_you[0]}), while `ops.waiting_on`"
            f" says it is waiting on {what!r} — {blocker['detail']}"
        )
    if not needs_you and what in USER_IS_NEEDED_WAITS:
        out.append(
            f"these two disagree and neither is overruled here: `ops.waiting_on` says "
            f"it is waiting on you ({what!r} — {blocker['detail']}), while `invariants."
            f"true_blockers` says nothing here needs you"
        )
    return out


#: The `waiting_on` answers that are NOT A BLOCKER: something is coming by itself (#227)
FIX_NOTHING_TO_CLEAR: tuple[str, ...] = (
    "turn_running", "validating", "retry_pending", "neo_question", "gate_with_neo",
    "queued_message", "pending", "manager_idle", "completed", "cancelled", "failed",
    "waiting_pr_merge", "needs_review",
)

#: Said of a blocker no shipped remedy covers AND whose way through somebody can type
FIX_UNCOVERED = ("no shipped remedy covers this blocker ({what}) — the OS is not offering "
                 "to act on it")
FIX_YOURS_TO_RUN = ("this is yours to run: the OS is not offering to take it, and nothing "
                    "here has been filed")
#: The same hand-back, labelled for the slugs whose way through the USER MAY NOT RUN
FIX_WORKERS_TO_RUN = ("this is the WORKER's to run in its own worktree, not yours: the OS "
                      "is not offering to take it, and nothing here has been filed")
#: CLOSED SET, `gate_held` its only member today
FIX_WORKER_MOVE: frozenset[str] = frozenset({"gate_held"})

#: CLOSED SET: the slugs whose own `detail` names a command THE USER MAY RUN
FIX_USERS_MOVE: frozenset[str] = frozenset({
    "signin", "assumptions", "plan_assumptions", "gate_escalated", "neo_escalated",
    "message_stuck",
})
FIX_NOTHING = "nothing here for a remedy to clear — {detail}"
FIX_LIVE_EDGES = ("every dependency edge it waits on is still LIVE, so the `unblock` "
                  "remedy would cut nothing and is not offered — only `--all` cuts a live "
                  "edge, and it then runs without the work it was told to build on")

#: What `confirm=False` has and has not done
FIX_APPROVING = ("nothing has been done and nothing has been filed. `jarvis wo fix "
                 "{wo_id} --remedy {remedy} --confirm` files a gate request for it; a "
                 "reviewer decides, and the OS applies it only after the gate opens")
FIX_OFFERED = ("one shipped remedy covers this blocker and this project has it armed — "
               "read what it touches before you confirm it")

#: `propose_fix`'s `reason`: WHY the OS is asking
FIX_REASON = "the user asked for this off `jarvis wo why {wo_id}`, which reported: {detail}"

#: The `argument` each matched remedy carries when the caller named none
FIX_ARGUMENTS = {
    "nudge": ("Say where you are. The OS's own diagnosis could not account for why this "
              "order has not moved, and nobody can see inside your turn."),
    "unblock": ("Cut the dependency edges that can never clear, so this order can be "
                "dispatched."),
}

#: THE DEFAULT ARM's answer for a slug `remedies.resolve` claims for nobody: a GAP CLASS
FIX_GAP_NO_INVESTIGATION = (
    "no registered remedy covers this blocker ({what}) — that gap class has no "
    "investigation open yet. Opening one on this order is the move that ends it: an "
    "analyst reads the gap and registers a remedy that clears `{what}` wherever it "
    "appears next, rather than a one-off fix for this order alone"
)
#: The same gap, but a LIVE improvement order already names this work order OR THIS GAP CLASS as...
FIX_GAP_INVESTIGATING = (
    "no registered remedy covers this blocker ({what}) — {io_id} is already investigating "
    "this gap class. Read it with `jarvis io show {io_id}`"
)

#: Neo q863(a): TITLE and BRIEF for the investigation `_fix_gap_note` opens on `confirm=True` with no...
FIX_GAP_INVESTIGATION_TITLE = (
    "Investigate the `{what}` gap class: no registered remedy clears it"
)
#: Neo's ruling condition on q863: the brief MUST end by filing a work order for a new REUSABLE remedy...
FIX_GAP_INVESTIGATION_BRIEF = (
    "`jarvis wo fix` found blocker `{what}` on {wo_id} with no registered remedy to clear "
    "it. `jarvis wo why {wo_id}` reported: {detail}\n\n"
    "End this investigation by filing a work order for a new REUSABLE remedy in "
    "src/jarvis/remedies.py: it needs tests, a `SHIPPED_REMEDIES` entry, and it must ship "
    "OFF `RemedyConfig`'s allow-list by default so the remedy arrives disarmed."
)
#: The just-opened case's own note
FIX_GAP_OPENED = (
    "no registered remedy covers this blocker ({what}) — {io_id} is now open to "
    "investigate this gap class. Read it with `jarvis io show {io_id}`"
)
#: A FAILURE IS NEVER A VERDICT (kn-40db1828): `create_improvement_order` can raise `OpsError`...
FIX_GAP_FILING_FAILED = (
    "no registered remedy covers this blocker ({what}) — an investigation could not be "
    "opened, and nothing was filed. Nothing was decided either: run this again"
)

#: A proposal the reviewer has been asked about, and one nobody could be asked about
FIX_FILED = ("gate request {approval} is filed and a reviewer decides it. The act happens "
             "only after the gate opens — `jarvis gate show {approval}`")
#: NO `{error}` SLOT
FIX_UNREACHABLE = ("the OS could not reach Neo to put this request to a reviewer, so "
                   "nothing was decided")
FIX_STILL_PENDING = ("gate request {approval} is filed and still pending, and no reviewer "
                     "has been asked yet — nothing was applied. Answer it yourself with "
                     "`jarvis gate approve {approval} --reason \"…\"`, or run this again "
                     "once Neo is reachable")
FIX_UNREACHABLE_NO_ROW = ("nothing was filed and nothing was applied — run it again once "
                          "Neo is reachable")


def remedy_config(project: str | None = None) -> Any:
    """The `supervisor.remedies` settings in force for `project`"""
    from .catalog import RemedyConfig

    try:
        catalog = resolve_catalog()
        spec: Any = catalog.os if project is None else catalog.project(project)
        return spec.supervisor.remedies
    except (OpsError, CatalogError, OSError, ValueError):
        return RemedyConfig()


def _fix_gap_note(project: str, wo_id: str, what: str, detail: str, confirm: bool) -> str:
    """§11's fallback for a slug `remedies.resolve` claims for nobody (#227)"""
    for io in list_improvement_orders(project, include_settled=False):
        refs = evidence_refs(io)
        if wo_id in refs or what in refs:
            return FIX_GAP_INVESTIGATING.format(what=what, io_id=io["id"])
    if not confirm:
        return FIX_GAP_NO_INVESTIGATION.format(what=what)
    # NO GATE (Neo q863(a), in Neo's own terms): the user pressing `--confirm` IS the authorisation
    try:
        io = create_improvement_order(
            project,
            FIX_GAP_INVESTIGATION_TITLE.format(what=what),
            FIX_GAP_INVESTIGATION_BRIEF.format(what=what, wo_id=wo_id, detail=detail),
            refs=[wo_id, what],
            # `WO_ORIGINS` (project_store.py) is a CLOSED set and this is the right member of it
            origin="jarvis")
    except OpsError as exc:
        # A FAILURE IS NEVER A VERDICT (kn-40db1828): the exception text stays here, never in the payload
        log.warning("could not open an investigation of the %s gap class on %s: %s",
                    what, wo_id, exc)
        return FIX_GAP_FILING_FAILED.format(what=what)
    return FIX_GAP_OPENED.format(what=what, io_id=io["id"])


def _fix_match(store: ProjectStore, wo: dict[str, Any], blocker: dict[str, Any],
               asked: str | None, project: str, confirm: bool
               ) -> tuple[str | None, str | None, dict[str, str] | None, str | None]:
    """`(remedy, note, your_move, argument)` for one blocker"""
    from . import remedies as remedies_mod

    wo_id = str(wo["id"])
    what = str(blocker["what"])
    # ASKED THE REGISTRY, not a table here: one home per rule
    match = None if asked is not None else remedies_mod.resolve(what)
    matched = asked if asked is not None else (match.remedy if match else None)
    # THE MAPPING OWNS THE RULE, on the asked path too: there is no `--force` here
    if matched == "nudge" and what in NUDGE_IS_WRONG:
        return None, f"{NUDGE_IS_WRONG[what]} {blocker['detail']}", None, None
    if asked is not None:
        return asked, None, None, None
    if matched == "unblock":
        edges = store.unfinished_dependencies(wo_id)
        if not edges:
            return None, FIX_NOTHING.format(detail=blocker["detail"]), None, None
        if not invariants.dead_dependencies(store, wo):
            return None, FIX_LIVE_EDGES, {
                "detail": f"jarvis wo unblock {wo_id} --all",
                "note": FIX_YOURS_TO_RUN}, None
    if matched is not None:
        assert match is not None
        return matched, None, None, match.params.get("argument")
    if what in FIX_NOTHING_TO_CLEAR:
        return None, FIX_NOTHING.format(detail=blocker["detail"]), None, None
    if what in FIX_USERS_MOVE or what in FIX_WORKER_MOVE:
        return None, FIX_UNCOVERED.format(what=what), {
            # §6's sentence, which the function that diagnosed the blocker wrote and which is already...
            "detail": blocker["detail"],
            # WHOSE move it is — `FIX_WORKER_MOVE`'s reason
            "note": (FIX_WORKERS_TO_RUN if what in FIX_WORKER_MOVE
                     else FIX_YOURS_TO_RUN)}, None
    # A SLUG NO REMEDY RESOLVES IS A GAP CLASS
    gap = _fix_gap_note(project, wo_id, what, blocker["detail"], confirm)
    return None, gap, None, None


def _file_fix(store: ProjectStore, project: str, wo: dict[str, Any], remedy_id: str,
              argument: str, reason: str, cfg: Any) -> dict[str, Any]:
    """File one proposal through `remedies.propose_fix` and report what came back (docs/superpowers/specs/2026-09-18-a-failure-is-not-an-answer.md §2)"""
    from . import remedies
    from .neo_store import NeoStore

    command = remedies.user_intent(str(wo["id"]), remedy_id, argument)
    try:
        neo = NeoStore()
    except Exception as exc:  # noqa: BLE001 — see docstring: never a synthesised verdict
        return _fix_unreachable(store, wo, command, exc)
    try:
        outcome = remedies.propose_fix(store, neo, project, wo, remedy_id, argument, cfg,
                                       reason=reason)
    except Exception as exc:  # noqa: BLE001 — same
        return _fix_unreachable(store, wo, command, exc)
    finally:
        neo.close()
    approval = outcome["approval"]
    question = outcome["question"]
    return {
        "proposed": bool(outcome["proposed"]), "reason": outcome["reason"],
        # IDS AND NOT THE ROWS
        "approval": approval["id"] if approval else None,
        "question": question["id"] if question else None,
        "unreachable": False,
        "note": (FIX_FILED.format(approval=approval["id"]) if outcome["proposed"]
                 else outcome["reason"]),
    }


def _fix_unreachable(store: ProjectStore, wo: dict[str, Any], command: str,
                     exc: Exception) -> dict[str, Any]:
    """`_file_fix`'s answer when nobody could be asked"""
    pending = next((row for row in store.pending_approvals(str(wo["id"]))
                    if row["command"] == command), None)
    log.warning("could not put a %s fix on %s to a reviewer: %s", command, wo["id"], exc)
    return {
        "proposed": False, "reason": FIX_UNREACHABLE,
        "approval": pending["id"] if pending else None, "question": None,
        "unreachable": True,
        "note": (FIX_STILL_PENDING.format(approval=pending["id"]) if pending
                 else FIX_UNREACHABLE_NO_ROW),
    }


def fix(wo_id: str, project_name: str | None = None, *, remedy: str | None = None,
        argument: str | None = None, confirm: bool = False) -> dict[str, Any]:
    """Clear the blocker §6 just named (docs/superpowers/specs/2026-09-24-order-observability.md)"""
    from . import remedies as remedies_mod

    name, path, wo = find_work_order(wo_id, project_name)
    cfg = remedy_config(name)
    store = ProjectStore(path)
    try:
        blocker = waiting_on(store, wo)
        matched, note, your_move, offered = _fix_match(store, wo, blocker, remedy, name,
                                                       confirm)
        # PRECEDENCE: the CALLER'S `--argument` wins
        arg = (argument or "").strip() or offered or FIX_ARGUMENTS.get(matched or "", "")
        if matched is not None:
            # THE CATALOG FIRST, `_config_refusal`'s own ordering: the user must never be shown something their...
            note = remedies_mod._config_refusal(
                matched, remedies_mod.subject_kind_of(wo), cfg)
            if note is None and matched == "file_work_order" and not arg:
                # `_apply_file_work_order`'s refusal, stated before a grant is filed instead of after one was spent
                note = remedies_mod.NO_ARGUMENT.format(origin=str(wo["id"]))
            if note is not None:
                matched = None
        payload: dict[str, Any] = {
            "wo_id": str(wo["id"]), "project": name, "title": wo["title"],
            "status": wo["status"], "blocker": blocker,
            "remedy": matched, "proposal": None, "your_move": your_move,
            "filed": None, "note": note or FIX_OFFERED,
        }
        if matched is None:
            return payload
        remedy_row = remedies_mod.REMEDIES[matched]
        payload["proposal"] = {
            "remedy": matched,
            # VERBATIM OFF THE REGISTRY
            "headline": remedy_row.headline, "blast": remedy_row.blast,
            "subject": str(wo["id"]), "argument": arg,
            "approving": FIX_APPROVING.format(wo_id=wo["id"], remedy=matched),
        }
        if confirm:
            payload["filed"] = _file_fix(
                store, name, wo, matched, arg,
                FIX_REASON.format(wo_id=wo["id"], detail=blocker["detail"]), cfg)
        return payload
    finally:
        store.close()


#: The bounded flags a surface may carry back when NOTHING WAS FILED
FIX_FLAG_NOTICES = {"unreachable": FIX_UNREACHABLE}

#: The other bounded shape a surface may carry back: `pending-<digits>`
FIX_PENDING_PREFIX = "pending-"


def fix_filed_notice(approval_id: int) -> str:
    """`FIX_FILED` for a page that has just redirected after filing a fix"""
    return FIX_FILED.format(approval=approval_id)


def fix_pending_notice(approval_id: int) -> str:
    """`fix_filed_notice` for the press nobody could be asked about, where the grant EXISTS"""
    return f"{FIX_UNREACHABLE} — {FIX_STILL_PENDING.format(approval=approval_id)}"


def fix_flag_notice(flag: str) -> str | None:
    """`fix_filed_notice` for the cases with no id to name — nothing was filed, or nobody could be asked"""
    return FIX_FLAG_NOTICES.get(flag)


def assume(wo_id: str, content: str) -> dict[str, Any]:
    """Record an assumption: DB row + ASSUMPTIONS.md append + conditional review flag (#711)"""
    from . import sections
    from .invariants import neo_reviews_later

    name, path, wo = find_work_order(wo_id)
    # ONE DECISION per assumption, refused never trimmed (docs/superpowers/specs/2026-10-21-questions-to-neo-are-atomic.md §2)
    cap = _concision_cap(name, "assume_max_chars", concision.DEFAULT_ASSUME_MAX_CHARS)
    if cap and len(content) > cap:
        raise OpsError(
            f"that assumption is {len(content)} characters; the cap is {cap}. An "
            f"assumption is ONE DECISION — the option you took, the strongest "
            f"alternative you rejected and why. Split it: a second decision is a "
            f"second `jarvis wo assume`, and cite this one in that one by its `#n` "
            f"(e.g. `per assumption #2`). Nothing was recorded.")
    _cited_blocks(name, path, wo, content)
    cited = _section_blocks(path, wo, content)
    store = ProjectStore(path)
    try:
        store.add_assumption(wo_id, content,
                             **({"spec_path": "\n".join(p for p, _, _ in cited),
                                 "spec_which": "\n".join(w for _, w, _ in cited),
                                 "spec_text": sections.label_sections(cited)}
                                if cited else {}))
        # An investigator's call is its diagnosis's audit trail, reviewed by nobody
        if kind_contract(wo).assumptions_reviewed and not neo_reviews_later(store, wo):
            store.flag_attention(wo_id, "assumptions pending review")
    finally:
        store.close()
    md = path / "ASSUMPTIONS.md"
    stamp = time.strftime("%Y-%m-%d")
    entry = f"- [ ] ({stamp}, {wo_id}) {content}\n"
    if md.exists():
        with md.open("a") as f:
            f.write(entry)
    else:
        md.write_text(
            f"# ASSUMPTIONS — {name}\n\n"
            "Assumptions made by worker agents, pending review. Managed by Jarvis.\n\n"
            + entry
        )
    return {"project": name, "wo_id": wo_id, "recorded": content}


def round_line(rnd: dict[str, Any]) -> str:
    """One validation round on one line: number, fingerprint, outcome, reason"""
    reason = (rnd.get("reason") or "").strip()
    # The COMMIT is the round's third input
    sha = str(rnd.get("head_sha") or "")
    # WHO OPENED IT, and only when the answer is not "a submission did"
    forced = (rnd.get("forced_reason") or "").strip()
    # WHAT THE ROUND FILED RATHER THAN BLOCKED ON
    filed = rnd.get("follow_ups") or NO_FOLLOW_UPS
    n_filed, dropped = len(filed.get("filed") or ()), int(filed.get("dropped") or 0)
    failed, n_kept = int(filed.get("failed") or 0), len(filed.get("withheld") or ())
    # Assembled as a LIST and joined
    parts = []
    if n_filed:
        parts.append(f"{n_filed} follow-up issue{'' if n_filed == 1 else 's'} filed")
    if n_kept:
        parts.append(f"{n_kept} kept internally")
    if dropped:
        parts.append(f"{dropped} over the cap")
    if failed:
        parts.append(f"{failed} not filed")
    note = f" · {', '.join(parts)}" if parts else ""
    # THE WORD, not the raw outcome: `failed` is three different facts and only `validation_standing`... (#581)
    word, _tone, _icon = validation_standing(rnd)
    # A REBIND, marked where the number is: round 4 under `max_rounds` 3 reads as the defect unless the...
    cause = str(rnd.get("uncounted_cause") or "")
    uncounted = (f" · uncounted ({cause})" if cause
                 else " · uncounted") if rnd.get("uncounted") else ""
    return (f"round {rnd['round']}{uncounted} · {rnd['fingerprint']} · {word}"
            f" · config {rnd.get('config_version') or 'not recorded'}"
            f" · commit {sha[:10] or 'not recorded'}"
            + note
            + (f" · forced: {forced}" if forced else "")
            + (f" — {reason}" if reason else ""))


#: How each `wo_alarms.status` reads to a person (docs/superpowers/specs/2026-08-31-the-supervisor.md)
ALARM_STANDING = {
    "raised": "raised",
    "informational": "a note, never escalated",
    "reviewing": "with the supervisor",
    "acked": "acked by the supervisor",
    "escalated": "escalated to Neo",
    "proposed": "a remedy proposed",
    "skipped": "not reviewed",
    "failed": "supervisor failed",
}


def turn_label(seq: int | None) -> str:
    """"turn 3", or "no turn" for a finding that judged a subject rather than a turn (docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md)"""
    return "no turn" if seq is None or seq == NO_TURN else f"turn {seq}"


def alarm_kind_label(alarm: dict[str, Any],
                     titles: dict[str, dict[str, str]]) -> str:
    """WHAT RAISED THIS ALARM, as user copy: a probe's title, or a cost alarm's kind"""
    if alarm.get("source") == "health" and alarm.get("probe"):
        by_project = titles.get(str(alarm.get("project") or ""), {})
        return by_project.get(str(alarm["probe"])) or str(alarm["probe"])
    return str(alarm.get("kind") or "")


def remedy_line(alarm: dict[str, Any]) -> str:
    """One alarm's remedy on one line, or "" when none was proposed (docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md)"""
    if not alarm.get("remedy"):
        return ""
    gate = (f" (gate request #{alarm['remedy_approval_id']})"
            if alarm.get("remedy_approval_id") else "")
    if alarm.get("remedy_result"):
        outcome = f"it did: {alarm['remedy_result']}"
    elif alarm.get("alarm_status") == "proposed":
        outcome = "nothing has been done — it needs your permission first"
    else:
        outcome = "nothing was done — the remedy was not granted"
    return (f"{alarm['remedy']}{gate}: {alarm.get('remedy_argument') or ''} "
            f"· {outcome}")


def alarm_standing_line(alarms: list[dict[str, Any]]) -> str:
    """One work order's alarms on one line: how many, how they stand, and their ids"""
    if not alarms:
        return ""
    counts = Counter(a["status"] for a in alarms)
    order = [*ALARM_STANDING, *sorted(k for k in counts if k not in ALARM_STANDING)]
    standing = ", ".join(f"{counts[s]} {ALARM_STANDING.get(s, s)}"
                         for s in order if counts.get(s))
    return f"{len(alarms)} ({standing}) — " + ", ".join(a["id"] for a in alarms)


#: The event one filing writes, and the only link between a round and the issues it produced (docs/superpowers/specs/2026-09-15-the-panel-blocks-on-blockers.md)
FOLLOW_UPS_EVENT = "validation_follow_ups_filed"

#: What a round with nothing filed projects
NO_FOLLOW_UPS: dict[str, Any] = {"filed": [], "withheld": [], "dropped": 0,
                                 "failed": 0}


def follow_up_key(title: Any) -> str:
    """The dedupe key for one finding's title: stripped, whitespace collapsed"""
    return " ".join(str(title or "").split())


#: The token that identifies one finding in BOTH published title shapes
FOLLOW_UP_TOKEN_RE = re.compile(r"\bvf-[0-9a-f]{8}\b")

#: What a withheld follow-up USED TO BE CALLED on a tracker the OS could not establish is private
WITHHELD_TITLE = "Validation follow-up"


def follow_up_digest(title: Any) -> str:
    """The stable per-finding token, derived from `follow_up_key(title)` and nothing else"""
    return "vf-" + hashlib.sha256(follow_up_key(title).encode()).hexdigest()[:8]


def follow_up_token(title: Any) -> str:
    """The token carried by a title already on the tracker, or "" if it carries none"""
    m = FOLLOW_UP_TOKEN_RE.search(str(title or ""))
    return m.group(0) if m else ""


def follow_up_claim(finding: Mapping[str, Any]) -> str:
    """THE DEDUPE KEY: the code a finding names, plus what it claims about it (#538)"""
    parts = [str(finding.get("file") or "").strip().lower(),
             str(finding.get("symbol") or "").strip(),
             follow_up_key(finding.get("title"))]
    return "|".join(p for p in parts if p)


def follow_up_digest_of(finding: Mapping[str, Any]) -> str:
    """One finding's token, over `follow_up_claim` rather than over its title alone"""
    return "vf-" + hashlib.sha256(follow_up_claim(finding).encode()).hexdigest()[:8]


#: What a follow-up must be about before it may become a tracker issue: a behaviour that is WRONG
def follow_up_admissible(finding: Mapping[str, Any]) -> bool:
    """Does this finding name a wrong behaviour, in a named file, with a failure?"""
    return bool(str(finding.get("file") or "").strip()
                and str(finding.get("failure") or "").strip())


def follow_up_feedback(follow_ups: Sequence[Mapping[str, Any]]) -> str:
    """The inadmissible findings, as a note for the SUBMITTER, or "\""""
    # Spec 2026-10-21 §3.1: a follow-up a seat rated `minor`/`cosmetic` is dropped
    lines = [f"- {follow_up_key(f.get('title'))}"
             for f in follow_ups if follow_up_key(f.get("title"))
             and f.get("level") not in ("minor", "cosmetic")
             and (not follow_up_admissible(f) or f.get("level") == "medium")]
    if not lines:
        return ""
    return ("\n\nAlso raised, and not blocking — worth fixing here rather than "
            "carrying:\n" + "\n".join(lines))


def follow_up_title(key: str, digest: str) -> str:
    """What the issue is called on the tracker: the finding's own title and its token"""
    return f"{key} [{digest}]"


def follow_up_repo(project_path: Path) -> str:
    """`owner/name` of the repository a follow-up for this project belongs on, or "\""""
    from . import github

    pair = github.origin_repo(project_path)
    return f"{pair[0]}/{pair[1]}" if pair else ""


def _steward_follow_ups(store: ProjectStore, project: ProjectSpec, unit_id: str,
                        pending: Sequence[Mapping[str, Any]],
                        payload: dict[str, Any],
                        context: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """The panel stops filing on its own say-so (spec 2026-10-21 §3.1, A5)"""
    from . import steward

    try:
        catalog = resolve_catalog()
    except Exception:  # noqa: BLE001 — see the docstring
        return list(pending)
    allowed, held = [], []
    decided: dict[int, dict[str, Any]] = {}
    for finding in pending:
        decision = steward.gate(
            steward.Candidate("panel", str(finding.get("level") or "medium"),
                              project.name, str(finding.get("title") or ""),
                              evidence=(str(finding.get("failure") or ""),),
                              parent_wo_id=unit_id,
                              file=str(finding.get("file") or ""),
                              # A held finding is filed LATER from this (`steward.drain`, #1181)
                              payload={**context, "finding": dict(finding)}),
            catalog=catalog, store=store, self_dispatch=True)
        kept = decided.get(decision.duplicate_of)
        if kept is not None and not decision.allowed:
            # One patch covers every seat's wording, so the brief carries them all (Q2109)
            kept.setdefault("also_worded", []).append(str(finding.get("title") or ""))
            held.append((finding, decision))
            continue
        row = dict(finding)
        if not decision.duplicate_of:
            decided[decision.decision_id] = row
        if decision.allowed:
            allowed.append(row)
        else:
            held.append((row, decision))
    if held:
        # `queued`: kept whole on the decision and filed once the steward has room (#1181)
        payload["steward_held"] = [
            {"title": f.get("title"), "level": f.get("level"),
             "decision_id": d.duplicate_of or d.decision_id, "outcome": d.outcome,
             "queued": d.outcome == "queue" and not d.duplicate_of}
            for f, d in held]
    return allowed


def reoffer_panel_follow_up(row: Mapping[str, Any], candidate: Any,
                            decision: Any) -> str:
    """File one queued panel finding the steward now has room for (`steward.drain`, #1181)"""
    from . import steward

    held = candidate.payload or {}
    finding = dict(held.get("finding") or {})
    if not finding:
        raise OpsError(f"steward decision #{row['id']} kept no finding to file")
    # Every seat's wording that folded onto it while it waited: one patch covers them all
    also = steward.candidate_wordings(row)
    if also:
        finding["also_worded"] = [*(finding.get("also_worded") or ()), *also]
    # The severer wording an escalation kept is the one filed
    finding["title"], finding["level"] = candidate.subject, decision.severity
    catalog = resolve_catalog()
    spec = project_spec(catalog, candidate.project)
    store = ProjectStore(spec.path)
    try:
        filed = file_validation_follow_ups(
            store, spec, {"round": int(held.get("round") or 0),
                          "id": int(held.get("round_id") or 0),
                          "pr_url": str(held.get("pr_url") or "")},
            [finding], spec.validation, wo_id=held.get("wo_id") or None,
            fo_id=held.get("fo_id") or None, steward_decided=True)
    finally:
        store.close()
    if filed["failed"]:
        # The tracker could not be read or written: still queued, offered again next pass
        raise OpsError(filed.get("reason") or "the follow-up could not be filed")
    urls = [i["url"] for i in filed["items"]]
    # Said on the decision's queue note: where the finding went, when not to a tracker issue
    return (urls[0] if urls else "kept on the internal record" if filed["withheld"]
            else "dropped over the follow-up cap" if filed["dropped"]
            else "already on the tracker")


def file_validation_follow_ups(store: ProjectStore, project: ProjectSpec,
                               round_row: Mapping[str, Any],
                               follow_ups: Sequence[Mapping[str, Any]],
                               cfg: Any, *, wo_id: str | None = None,
                               fo_id: str | None = None,
                               steward_decided: bool = False) -> dict[str, Any]:
    """Keep this unit's non-blocking findings — on the tracker if they may travel there"""
    from . import issues

    n, round_id = int(round_row["round"]), int(round_row["id"])
    unit_id = wo_id or fo_id or ""
    payload: dict[str, Any] = {"round": n, "round_id": round_id, "unit": unit_id,
                               "items": [], "withheld": [], "dropped": 0, "failed": 0}
    # `follow_ups` is read defensively by the CALLER too (`verdict.get(...) or ()`): `Daemon.validator`...
    if not getattr(cfg, "follow_ups", True) or not follow_ups:
        return payload

    # ADMISSIBLE, DISTINCT, and no network yet: two seats can reach the same nit in one round
    pending = _distinct([f for f in follow_ups if follow_up_admissible(f)])
    if not pending:
        return payload
    if not steward_decided:
        # `steward_decided`: the steward already said yes to these — a re-offered queue
        pending = _steward_follow_ups(
            store, project, unit_id, pending, payload,
            {"round": n, "round_id": round_id, "pr_url": str(round_row.get("pr_url") or ""),
             "wo_id": wo_id, "fo_id": fo_id})
    if not pending:
        return _record_filing(store, payload, wo_id=wo_id, fo_id=fo_id)

    kept = {str(row["digest"]) for row in store.internal_follow_ups(unit_id)}
    # THE ORDER'S OWN HISTORY, both halves, both local
    raised = len(kept) + len([r for r in store.issue_links_of(unit_id)
                              if r["kind"] == "raised"])
    cap = int(getattr(cfg, "max_follow_ups", DEFAULT_VALIDATION_FOLLOW_UP_CAP))
    room = max(0, cap - raised)

    repo = follow_up_repo(project.path)
    # ONE privacy read, and it fails closed: anything but a positive "this repository is private"...
    private = bool(repo) and issues.repo_is_private(repo)
    if not private:
        # AND NOT ONE THE TRACKER ALREADY CARRIES
        filed_keys = set(_filed_titles(store, unit_id).values())
        fresh = [f for f in pending if follow_up_digest_of(f) not in kept
                 and follow_up_key(f.get("title")) not in filed_keys]
        payload["dropped"] = max(0, len(fresh) - room)
        for finding in fresh[:room]:
            key = follow_up_key(finding.get("title"))
            digest = follow_up_digest_of(finding)
            seat = str(finding.get("seat") or "")
            store.record_internal_follow_up(
                digest, unit_id, round=n, seat=seat, title=key,
                detail=str(finding.get("detail") or ""),
                file=str(finding.get("file") or ""),
                symbol=str(finding.get("symbol") or ""),
                failure=str(finding.get("failure") or ""))
            payload["withheld"].append({"digest": digest, "title": key, "seat": seat})
        if payload["withheld"]:
            payload["reason"] = (f"{repo or 'the project'} is not a repository this OS "
                                 f"could establish is private, so the findings are kept "
                                 f"on the internal record")
        return _record_filing(store, payload, wo_id=wo_id, fo_id=fo_id)

    try:
        # ONE round trip for the dedupe, whatever the cap
        already, already_keys = set(), set()
        for row in issues.follow_ups_filed(repo, unit_id):
            already.add(follow_up_token(row.get("title")))
            already_keys.add(follow_up_key(row.get("title")))
        already.discard("")
    except GitHubError as e:
        # The tracker could not be READ, so nothing may be written: filing blind would duplicate every...
        log.info("[%s] %s: could not read %s for dedupe: %s",
                 project.name, unit_id, repo, e)
        payload["failed"] += len(pending)
        payload["reason"] = "the tracker could not be read"
        return _record_filing(store, payload, wo_id=wo_id, fo_id=fo_id)

    # DEDUPE BEFORE THE CAP, AND THE ORDER IS THE WHOLE CORRECTNESS OF THIS
    fresh = [f for f in pending
             if follow_up_digest_of(f) not in already
             and follow_up_digest_of(f) not in kept
             and follow_up_digest(f.get("title")) not in already
             and follow_up_key(f.get("title")) not in already_keys]
    payload["dropped"] = max(0, len(fresh) - room)
    if not fresh[:room]:
        return _record_filing(store, payload, wo_id=wo_id, fo_id=fo_id)

    pr_url = str(round_row.get("pr_url") or "")
    for finding in fresh[:room]:
        key = follow_up_key(finding.get("title"))
        seat = str(finding.get("seat") or "")
        title = follow_up_title(key, follow_up_digest_of(finding))
        body = _follow_up_body(finding, unit_id=unit_id, round_no=n, pr_url=pr_url,
                               seat=seat)
        try:
            url = issues.file_follow_up(repo, title, body)
        except GitHubError as e:
            log.info("[%s] %s: filing %r on %s failed: %s",
                     project.name, unit_id, title, repo, e)
            payload["failed"] += 1
            continue
        # `title` ON THE EVENT IS THE FINDING'S OWN, never what the tracker was told
        payload["items"].append({"url": url, "number": issues.issue_number(url),
                                 "title": key, "seat": seat, "withheld": False})
        # THE RELATION, beside the event
        store.record_issue(url, number=issues.issue_number(url), repo=repo, title=title,
                           state="OPEN")
        store.link_issue(url, unit_id, "raised", round_no=n, seat=seat, announced=True)
    return _record_filing(store, payload, wo_id=wo_id, fo_id=fo_id)


def _distinct(follow_ups: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The findings worth trying to file: titled, and one per normalised title"""
    kept: list[Mapping[str, Any]] = []
    seen: set[str] = set()
    for finding in follow_ups:
        title = follow_up_key(finding.get("title"))
        if not title or title in seen:
            continue
        seen.add(title)
        kept.append(finding)
    return kept


def _record_filing(store: ProjectStore, payload: dict[str, Any], *,
                   wo_id: str | None, fo_id: str | None) -> dict[str, Any]:
    """Write the event, unless the round had nothing at all to say"""
    if (payload["items"] or payload["withheld"] or payload["dropped"]
            or payload["failed"] or payload.get("steward_held")):
        if wo_id:
            store.add_event(wo_id, FOLLOW_UPS_EVENT, payload)
        else:
            feature_event(store, str(fo_id), FOLLOW_UPS_EVENT,
                          {**payload, "feature_order": fo_id})
    return payload


def _follow_up_body(finding: Mapping[str, Any], *, unit_id: str, round_no: int,
                    pr_url: str, seat: str) -> str:
    """The issue body ON A PRIVATE TRACKER: the finding's own detail, then where it came from"""
    detail = str(finding.get("detail") or "").strip()
    # WHAT MAKES THE ISSUE ACTIONABLE
    where = " ".join(x for x in (str(finding.get("file") or "").strip(),
                                 str(finding.get("symbol") or "").strip()) if x)
    failure = str(finding.get("failure") or "").strip()
    # The wordings the steward collapsed onto this one: one patch covers them all (Q2109)
    also = [str(t).strip() for t in finding.get("also_worded") or () if str(t).strip()]
    footer = [
        "",
        "---",
        f"Raised by the Jarvis validation panel's `{seat or 'panel'}` seat in round "
        f"{round_no} of `{unit_id}`, as a follow-up rather than a blocker — the review "
        f"judged the work shippable without it.",
    ]
    if pr_url:
        footer.append(f"Pull request: {pr_url}")
    return "\n".join([detail or "_The seat recorded no detail._",
                      *([f"\n**Where:** `{where}`"] if where else []),
                      *([f"\n**Failure:** {failure}"] if failure else []),
                      *(["\n**Other seats worded the same defect as:**",
                         *(f"- {t}" for t in also)] if also else []),
                      *footer])


def filed_follow_ups(store: ProjectStore, *, wo_id: str | None = None,
                     fo_id: str | None = None) -> dict[int, dict[str, Any]]:
    """What the panel filed, per round id, for the surfaces that print a round"""
    rows = (store.events_of_kind(wo_id, FOLLOW_UPS_EVENT) if wo_id
            else feature_events_of_kind(store, str(fo_id), FOLLOW_UPS_EVENT))
    if not rows:
        return {}
    out: dict[int, dict[str, Any]] = {}
    for row in rows:
        p = db.from_json(row["payload"], {})
        # A round RETRIED after a transport failure files again and writes a second event
        rnd = out.setdefault(int(p.get("round_id") or 0),
                             {"filed": [], "withheld": [], "dropped": 0, "failed": 0})
        for item in p.get("items") or ():
            if isinstance(item, Mapping) and item.get("url"):
                rnd["filed"].append(dict(item))
        # KEPT, NOT FILED
        for item in p.get("withheld") or ():
            if isinstance(item, Mapping) and item.get("title"):
                rnd["withheld"].append(dict(item))
        rnd["dropped"] = int(p.get("dropped") or 0)
        rnd["failed"] = int(p.get("failed") or 0)
        if p.get("reason"):
            rnd["reason"] = str(p["reason"])
    return out


# -- the issue index: which orders point at which issues, and how many

#: A `#N` in a brief
ISSUE_MENTION_RE = re.compile(r"(?<![\w/])#([0-9]{1,7})(?![0-9])")

#: SOMETHING ISSUE-URL-SHAPED in a body of prose
ISSUE_LINK_RE = re.compile(
    r"https://[A-Za-z0-9.-]+/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/issues/([0-9]+)(?![0-9])")


def issue_url_for(repo: str, number: int) -> str:
    """THE ONE DEFINITION of what a project's own issue URL looks like — host included (#7)"""
    return f"https://github.com/{repo}/issues/{number}"


def is_own_issue_url(url: str, repo: str, number: int) -> bool:
    """Case-insensitive because GitHub is; host, owner, repository and number all count"""
    return (url or "").lower() == issue_url_for(repo, number).lower()


def issue_citations(text: str, repo: str, known: Collection[str]) -> list[str]:
    """Every issue on `repo` that `text` deliberately cites"""
    body = text or ""
    found: dict[str, None] = {}
    for match in ISSUE_LINK_RE.finditer(body):
        number = int(match.group(1))
        if is_own_issue_url(match.group(0), repo, number):
            found.setdefault(issue_url_for(repo, number), None)
    for match in ISSUE_MENTION_RE.finditer(body):
        url = issue_url_for(repo, int(match.group(1)))
        if url in known:
            found.setdefault(url, None)
    return list(found)


def record_issue_references(store: ProjectStore, project_path: Path,
                            wo: Mapping[str, Any]) -> list[str]:
    """Link a freshly created work order to the issues its brief points at"""
    from . import issues

    repo = follow_up_repo(project_path)
    if not repo:
        return []
    linked: list[str] = []
    known = {row["issue_url"] for row in store.issue_board()}
    wanted: list[tuple[str, str]] = []
    assigned = str(wo.get("issue_url") or "")
    if assigned and issues_on(assigned, repo):
        wanted.append((assigned, "assigned"))
    brief = f"{wo.get('title') or ''}\n{wo.get('description') or ''}"
    wanted += [(url, "cited") for url in issue_citations(brief, repo, known)]
    for url, kind in wanted:
        try:
            store.record_issue(url, number=issues.issue_number(url), repo=repo)
            store.link_issue(url, wo["id"], kind)
        except (ValueError, sqlite3.Error):
            log.exception("could not link %s to %s", url, wo.get("id"))
            continue
        linked.append(url)
    return linked


def issues_on(url: str, repo: str) -> bool:
    """Is `url` an issue URL on `repo`?"""
    from .issues import ISSUE_URL_RE

    match = ISSUE_URL_RE.match(url or "")
    return bool(match) and is_own_issue_url(url, repo, int(match.group(4)))


#: What a unit with no linked issue projects
NO_ISSUES: dict[str, Any] = {"raised": [], "references": [], "withheld": []}


def issue_index(store: ProjectStore, unit_id: str) -> dict[str, Any]:
    """Every issue one order raised, and every other issue it points at"""
    titles = _filed_titles(store, unit_id)
    # WITHHELD FOLLOW-UPS ARE NOT ISSUES AND ARE STILL THIS ORDER'S
    withheld = [{"digest": row["digest"], "title": row["title"], "seat": row["seat"],
                 "round": row["round"] or 0, "file": row["file"],
                 "symbol": row["symbol"], "failure": row["failure"],
                 "detail": row["detail"]}
                for row in store.internal_follow_ups(unit_id)]
    raised, refs = [], []
    for row in store.issue_links_of(unit_id):
        (raised if row["kind"] == "raised" else refs).append({
            "url": row["issue_url"], "number": row["number"] or 0,
            "title": titles.get(row["issue_url"]) or row["title"] or "",
            "state": row["state"] or "",
            "kind": row["kind"], "round": row["round"] or 0,
            "seat": row["seat"] or ""})
    return {"raised": raised, "references": refs, "withheld": withheld}


def _filed_titles(store: ProjectStore, unit_id: str) -> dict[str, str]:
    """`{issue url: the finding's own title}` for everything this unit filed"""
    kind = {"fo_id": unit_id} if is_feature_order_id(unit_id) else {"wo_id": unit_id}
    out: dict[str, str] = {}
    for payload in filed_follow_ups(store, **kind).values():  # type: ignore[arg-type]
        for item in payload.get("filed") or []:
            url, title = str(item.get("url") or ""), str(item.get("title") or "")
            if url and title:
                out[url] = title
    return out


def issue_board(store: ProjectStore, limit: int = 50) -> list[dict[str, Any]]:
    """This project's tracked issues, most-referenced first"""
    rows = [{**row, "internal": False} for row in store.issue_board()
            if (row.get("state") or "") != "CLOSED"]
    return (rows + internal_board(store))[:limit]


def internal_board(store: ProjectStore) -> list[dict[str, Any]]:
    """The withheld follow-ups, in the shape `issue_board` rows have"""
    return [{"issue_url": "", "number": 0, "repo": "", "title": row["title"],
             "state": "", "refs": 1, "internal": True, "digest": row["digest"],
             "units": [{"unit_id": row["unit_id"], "kind": "raised",
                        "round": row["round"] or 0, "seat": row["seat"] or ""}]}
            for row in store.internal_follow_ups()]


def issue_board_across(project_name: str | None = None,
                       limit: int = 50) -> list[dict[str, Any]]:
    """`issue_board` for one project or for the whole fleet, ranked across all of them"""
    paths = registered_project_paths()
    if project_name:
        if project_name not in paths:
            raise OpsError(f"project {project_name!r} not registered "
                           f"(known: {sorted(paths)})")
        paths = {project_name: paths[project_name]}
    out: list[dict[str, Any]] = []
    for name, path in paths.items():
        if not path.is_dir():
            continue
        store = ProjectStore(path)
        try:
            out += [{**row, "project": name} for row in issue_board(store, limit=limit)]
        finally:
            store.close()
    out.sort(key=lambda row: (-int(row["refs"]), bool(row.get("internal")),
                              -int(row["number"] or 0)))
    return out[:limit]


def validation_rounds(store: ProjectStore, *, wo_id: str | None = None,
                      fo_id: str | None = None) -> list[dict[str, Any]]:
    """One unit's rounds, oldest first, WITHOUT the seats' opinions"""
    filed = filed_follow_ups(store, wo_id=wo_id, fo_id=fo_id)
    # `uncounted` is in the tuple because `round_line` must mark a rebind: a round numbered past...
    return [{**{k: r[k] for k in ("id", "round", "ts", "fingerprint", "outcome",
                                  "reason", "pr_url", "config_version", "head_sha",
                                  "forced_reason", "hold_cause", "uncounted",
                                  "uncounted_cause")},
             # ALWAYS PRESENT, even empty — the rule this key list, `assumptions` and `alarms` already follow
             "follow_ups": filed.get(int(r["id"]), NO_FOLLOW_UPS)}
            for r in store.validation_rounds(wo_id=wo_id, fo_id=fo_id)]


#: Every event the automatic merge writes
AUTOMERGE_EVENTS = ("automerge_merged", "automerge_decided", "automerge_proposed",
                    "automerge_failed", "automerge_held")

#: The one event that is TERMINAL: nothing follows a merge
AUTOMERGE_TERMINAL = "automerge_merged"


def automerge_checks_hold_spans(events: Iterable[Any], since: float, now: float,
                                open_end: float) -> list[tuple[float, float]]:
    """Every interval inside `[since, now]` the OS held a merge for GitHub's checks (docs/superpowers/specs/2026-10-20-a-merge-held-for-running-checks-is-not-stuck.md §4)"""
    from . import db
    from .automerge import HELD_CHECKS_RUNNING

    rows = sorted(events, key=lambda e: float(e["ts"]))
    spans: list[tuple[float, float]] = []
    for n, e in enumerate(rows):
        if str(e["kind"]) != "automerge_held":
            continue
        payload = db.from_json(e["payload"], {}) or {}
        if str(payload.get("code") or "") != HELD_CHECKS_RUNNING:
            continue
        nxt = next((r for r in rows[n + 1:] if str(r["kind"]) in AUTOMERGE_EVENTS), None)
        # Nothing dates the end of an open hold but the horizon (§4)
        ends = [open_end, now] + ([float(nxt["ts"])] if nxt is not None else [])
        start, end = max(float(e["ts"]), since), min(ends)
        if end > start:
            spans.append((start, end))
    return sorted(spans)


def merge_state(store: ProjectStore, wo: dict[str, Any]) -> dict[str, Any] | None:
    """What the surfaces say about this order's pull request landing, or None"""
    from . import db

    pr_url = str(wo.get("pr_url") or "")
    merged = store.events_of_kind(wo["id"], "pr_merged")
    if not pr_url and not merged:
        return None
    if merged:
        payload = db.from_json(merged[-1]["payload"], {})
        return {"pr_url": str(payload.get("pr_url") or pr_url), "state": "MERGED",
                "head_oid": str(payload.get("head_oid") or ""),
                "merged_at": payload.get("merged_at"),
                "source": str(payload.get("source") or "")}
    seen = store.events_of_kind(wo["id"], "landing_seen")
    payload = db.from_json(seen[-1]["payload"], {}) if seen else {}
    return {"pr_url": pr_url, "state": str(payload.get("pr_state") or ""),
            "head_oid": "", "merged_at": None, "source": ""}


def self_evolution_state(wo: dict[str, Any]) -> dict[str, Any] | None:
    """What `jarvis wo show` says about a FIX ORDER's four columns, or None (docs/superpowers/specs/2026-09-27-self-evolution.md)"""
    gap = str(wo.get("gap_class") or "").strip()
    if not gap:
        return None
    detector_id = str(wo.get("detector_id") or "").strip()
    remedy_rule_id = str(wo.get("remedy_rule_id") or "").strip()
    trigger_wo_id = str(wo.get("trigger_wo_id") or "").strip()

    detector: dict[str, Any] | None = None
    if detector_id:
        central = CentralStore()
        try:
            detector = central.get_detector(detector_id)
        finally:
            central.close()

    if not detector_id:
        detector_note = "no detector named yet"
    elif detector is None:
        detector_note = f"detector {detector_id} — proposed, not yet registered"
    else:
        detector_note = (f"detector {detector_id} — registered, "
                         f"{detector.get('status') or 'dry_run'}")

    parts = [f"gap class {gap}", detector_note]
    if remedy_rule_id:
        parts.append(f"remedy rule {remedy_rule_id}")
    parts.append(f"proved against {trigger_wo_id}" if trigger_wo_id
                 # The refusal `rules.precheck` would print names the same argument
                 else "no triggering order named (`jarvis wo create --trigger-wo-id`)")
    return {"gap_class": gap, "detector_id": detector_id,
            "remedy_rule_id": remedy_rule_id, "trigger_wo_id": trigger_wo_id,
            "detector_registered": detector is not None,
            "detector_status": str((detector or {}).get("status") or ""),
            "line": " · ".join(parts)}


def automerge_state(store: ProjectStore, wo: dict[str, Any]) -> dict[str, Any] | None:
    """What `jarvis wo show` and the dashboard say about the automatic merge, or None"""
    from . import db
    from .automerge import decided_sha

    newest: dict[str, Any] | None = None
    terminal: dict[str, Any] | None = None
    decided: dict[str, Any] | None = None
    for kind in AUTOMERGE_EVENTS:
        rows = store.events_of_kind(wo["id"], kind)
        if not rows:
            continue
        # The payload is spread FIRST so that `kind` and `ts` are this function's own answers and not...
        candidate = {**db.from_json(rows[-1]["payload"], {}),
                     "kind": kind, "ts": float(rows[-1]["ts"])}
        if kind == AUTOMERGE_TERMINAL:
            terminal = candidate
        else:
            if kind == "automerge_decided":
                decided = candidate
            if newest is None or candidate["ts"] > newest["ts"]:
                newest = candidate
    if (newest is not None and newest["kind"] == "automerge_held"
            and decided is not None and decided.get("decision") != "approved"
            and decided_sha(decided)
            and decided_sha(decided) == str(newest.get("head_sha") or "")):
        newest = decided
    newest = terminal or newest
    if newest is None:
        return None
    # A HOLD IS MARKED STALE, NEVER DROPPED (2026-09-27-a-stale-merge-hold-is-not-the-...
    if newest["kind"] == "automerge_held":
        latest = store.latest_validation_round(wo_id=wo["id"])
        if why := _automerge_hold_is_stale(wo, latest, newest):
            newest = {**newest, "stale_because": why,
                      "stale_status": str(wo.get("status") or ""),
                      "stale_round": int((latest or {}).get("round") or 0)}
            # `rejudging` REWORDS ONLY and never sets `stale`: `force_validation_state` blanks its diagnosis on...
            if why != "rejudging":
                newest["stale"] = True
    return {**newest, "line": _automerge_line(newest)}


#: Every event the automatic assumption review writes
AUTOREVIEW_EVENTS = ("autoreview_accepted", "autoreview_escalated", "autoreview_asked",
                     "autoreview_held", "autoreview_provisional", "autoreview_objected",
                     "autoreview_objection_withdrawn", "autoreview_confirmed",
                     "autoreview_unconfirmed")

#: The kinds whose prose asserts the USER OWES A DECISION (docs/superpowers/specs/2026-09-25-a-decided-assumption-is-not-left-with-you.md)
_AUTOREVIEW_OWED_BY_USER = ("autoreview_escalated", "autoreview_unconfirmed")


def autoreview_state(store: ProjectStore, wo: dict[str, Any]) -> dict[str, Any] | None:
    """What `jarvis wo show` and the dashboard say about the automatic review, or None"""
    from . import db

    newest: dict[str, Any] | None = None
    stale = _stale_panel_hold(store, wo["id"], status=str(wo.get("status") or ""))
    for kind in AUTOREVIEW_EVENTS:
        rows = store.events_of_kind(wo["id"], kind)
        # A `panel_gave_up` hold about an overtaken round cannot be this kind's candidate
        if kind == "autoreview_held":
            rows = [r for r in rows
                    if not stale(kind, db.from_json(r["payload"], {}))]
        if not rows:
            continue
        # Payload first, so `kind` and `ts` are this function's answers and not whatever an event happened to...
        candidate = {**db.from_json(rows[-1]["payload"], {}),
                     "kind": kind, "ts": float(rows[-1]["ts"])}
        if newest is None or candidate["ts"] > newest["ts"]:
            newest = candidate
    if newest is None:
        return None
    # An ask is a claim about a question
    if newest["kind"] == "autoreview_asked":
        gone = _unreachable_asks()(newest.get("neo_question_id"))
        if gone:
            newest = {**newest, "unreachable": gone}
    # "left with you" is a claim about the PRESENT: resolve it against the row
    if newest["kind"] in _AUTOREVIEW_OWED_BY_USER:
        aid = int(newest.get("assumption_id") or 0)
        row = store.get_assumption(aid) if aid else None
        status = str((row or {}).get("status") or "")
        if status and status != "pending":
            newest = {**newest, "resolved_status": status,
                      "resolved_decider": assumption_decider(row or {})}
    return {**newest, "line": _autoreview_line(newest)}


def review_state(store: ProjectStore, wo: dict[str, Any]) -> dict[str, Any] | None:
    """The decision this work order owes the user, and what each button would do — or None (docs/superpowers/specs/2026-09-27-a-review-control-for-an-escalated-round.md §2.)"""
    from .invariants import validation_escalated

    pending_rows = store.pending_assumptions(wo["id"])
    pending = len(pending_rows)
    escalated = (wo["status"] == "needs_review" and validation_escalated(store, wo))
    if not pending and not escalated:
        return None
    # THE ENRICHED ROWS, and only when a pending row actually carries an objection
    objected = objected_rows(
        [a for a in assumptions_with_rulings(store, wo["id"])
         if str(a.get("status") or "") == "pending"]
        if any(str(a.get("provisional_verdict") or "") == "object"
               and a.get("objection_envelope_id") for a in pending_rows) else [])
    # The same row the boolean came from (docs/superpowers/specs/2026-10-14-a-message-must-not-drop-an-owed-review.md)
    latest = store.last_judged_round(wo_id=wo["id"]) if escalated else None
    # WHY THE PANEL HANDED IT OVER, and the command this order's shape accepts (docs/superpowers/specs/2026-10-14-an-escalated-round-says-why-and-what-to-run-next.md §3)
    cause = (escalation_cause(store, wo_id=wo["id"], round_id=int(latest["id"]))
             if latest else None)
    family = ESCALATION_FAMILY[cause] if cause else None
    rows = f"{pending} pending assumption" + ("" if pending == 1 else "s")
    # Only a `handover` round carries a panel opinion
    landing = ("" if not escalated else
               " over the panel's objection" if family == "handover"
               else " without a panel verdict")
    accept = ("Accepts " + rows + " and lands" if pending else "Lands") + \
        " the work order" + landing
    reject = ("Rejects " + rows + " and resumes" if pending else "Resumes") + \
        " the worker, with your reason as the guidance it is sent back with"
    return {
        "pending": pending,
        "escalated": escalated,
        "round": int(latest["round"]) if latest else None,
        "cause": cause,
        "family": family,
        "verb": ESCALATION_VERB[family] if family else None,
        "why": (ESCALATION_WHY[cause].format(n=int(latest["round"]),
                                             rebind_max=REBIND_MAX,
                                             bounce_limit=BOUNCE_LIMIT)
                if cause else None),
        "next": escalation_next(wo, family) if family else [],
        "placement": "assumptions" if pending else "validation",
        "scope": "One decision, and it settles the whole order — every pending "
                 "assumption and the order itself, in a single call",
        "accept": accept + ".",
        "reject": reject + ".",
        "strands": "Rejecting with no reason leaves the work order flagged and the "
                   "worker unguided.",
        # THE OBJECTED ROWS THE USER IS ABOUT TO RULE ON
        **({"objected": objected,
            "objected_note": objected_note(objected)} if objected else {}),
    }


#: What happened to the objection on an objected row
OBJECTION_DELIVERY_STATES = ("delivered", "handed_back", "withdrawn", "in_flight")

#: The delivery states that mean the worker never read the objection
OBJECTION_UNTOLD_STATES = ("handed_back", "withdrawn", "in_flight")


def objected_rows(pending: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The pending assumptions the OS OBJECTED to, structured for `review_state`"""
    out = []
    for a in pending:
        if str(a.get("provisional_verdict") or "") != "object":
            continue
        if not a.get("objection_envelope_id"):
            continue
        if a.get("objection_delivered_ts"):
            delivery = "delivered"
        elif a.get("objection_withdrawn_ts"):
            # WHICH withdrawal, which is the fact the user cannot otherwise get: taken back because the order...
            delivery = ("handed_back"
                        if str(a.get("objection_withdrawn_reason") or "")
                        == OBJECTION_HANDED_BACK_REASON else "withdrawn")
        else:
            delivery = "in_flight"
        out.append({"n": a.get("n"), "reason": str(a.get("provisional_reason") or ""),
                    "delivery": delivery})
    return out


def objected_note(objected: list[dict[str, Any]]) -> str:
    """One sentence, in Python because a phrase in Jinja cannot reach a terminal (§4)"""
    def which(rows: list[dict[str, Any]]) -> str:
        return ", ".join(f"#{o['n']}" for o in rows)

    untold = [o for o in objected if o["delivery"] in OBJECTION_UNTOLD_STATES]
    told = [o for o in objected if o["delivery"] == "delivered"]
    noun = "assumption" if len(objected) == 1 else "assumptions"
    tail = "decide it against the result this order delivered"
    if untold and told:
        return (f"the OS objected to {noun} {which(untold)}, which the worker was never "
                f"told, and to {which(told)}, which it was told and left as it is — "
                f"{tail}")
    if told:
        return (f"the OS objected to {noun} {which(told)} and the worker was told and "
                f"left it as it is — {tail}")
    return (f"the OS objected to {noun} {which(untold)} and the worker was never told "
            f"— {tail}")


def escalation_next(wo: dict[str, Any], family: str) -> list[str]:
    """The commands this escalated order accepts, in the order a reader can act (docs/superpowers/specs/2026-10-14-an-escalated-round-says-why-and-what-to-run-next.md §4.)"""
    wo_id = str(wo["id"])
    if family == "handover":
        lines = [
            f'jarvis wo review {wo_id} --reject --feedback "<what to change>" — resumes '
            f"the worker with the panel's asks as its guidance.",
            f'jarvis wo review {wo_id} --feedback "<why>" — lands it over the panel\'s '
            f"objection.",
        ]
    else:
        lines = [
            f'jarvis wo review {wo_id} --reject --feedback "<what to change>" — resumes '
            f"the worker with your reason as its guidance.",
            f'jarvis wo review {wo_id} --feedback "<why>" — lands it without a panel '
            f"verdict.",
        ]
    if family == "unjudged" and str(wo.get("pr_url") or ""):
        lines.append(
            f'jarvis validation force {wo_id} --reason "<why>" — judges the current pull '
            f"request again; worth it only because no seat ever judged this one.")
    return lines


def review_lines(state: dict[str, Any]) -> list[str]:
    """The decision this order owes, as lines — THE ONE BUILDER both CLI surfaces print (docs/superpowers/specs/2026-10-14-an-escalated-round-says-why-and-what-to-run-next.md §5)"""
    owed = "pending assumption" + ("" if state["pending"] == 1 else "s")
    where = (f"round {state['round']} {state['verb']}" if state["escalated"]
             else f"{state['pending']} {owed}")
    lines = [f"owed: {where} — {state['scope']}."]
    # THE LINK AT THE BOUNDARY (§4 of 2026-10-20-an-objection-is-withdrawn-before-the- order-stops.md)
    if state.get("objected_note"):
        lines.append(f"objected: {state['objected_note']}.")
    if state.get("why"):
        lines.append(f"why: {state['why']}")
    lines += [f"next: {command}" for command in state.get("next") or []]
    return lines + [f"accept: {state['accept']}",
                    f"reject: {state['reject']} {state['strands']}"]


def assumption_decider(a: dict[str, Any]) -> str:
    """WHO decided this assumption, in words, for whoever is about to read the verdict"""
    by = str(a.get("decided_by") or "")
    if not by and str(a.get("status") or "") == "pending" \
            and str(a.get("provisional_verdict") or ""):
        model = a.get("provisional_model") or "model not recorded"
        return f"nobody yet — the OS ({ASSUMPTION_DECIDER_OS}, {model}) has only a " \
               f"provisional reading"
    by = by or ASSUMPTION_DECIDER_USER
    if by == ASSUMPTION_DECIDER_USER:
        return "you"
    return f"the OS ({by}, {a.get('decided_model') or 'model not recorded'})"


#: HOW FINAL AN `autoreview` EVENT IS about the assumption it names, for ties on `ts`
_RULING_RANK = {"autoreview_confirmed": 9, "autoreview_unconfirmed": 8,
                "autoreview_accepted": 7, "autoreview_escalated": 6,
                "autoreview_held": 5, "autoreview_objection_withdrawn": 4,
                "autoreview_objected": 3, "autoreview_provisional": 2,
                "autoreview_asked": 1}


#: THE STATUSES A FRESH HOLD CAN STILL ARRIVE IN, and freshness is judged ONLY outside them
_HOLD_REFRESHABLE_STATUSES = ("waiting_pr_merge", "validating")


def _automerge_hold_is_stale(wo: dict[str, Any], latest_round: dict[str, Any] | None,
                             payload: dict[str, Any]) -> str:
    """WHY this `automerge_held` event is no longer a claim about now — `""` when it is (#786)"""
    status = str(wo.get("status") or "")
    if status not in _HOLD_REFRESHABLE_STATUSES:
        return "status"
    if status != "waiting_pr_merge":
        return "rejudging"
    held_round = int(payload.get("round") or 0)
    if held_round and held_round < int((latest_round or {}).get("round") or 0):
        return "round"
    return ""


def _panel_hold_is_stale(latest_round: dict[str, Any] | None,
                         payload: dict[str, Any], status: str = "",
                         assumption: dict[str, Any] | None = None) -> bool:
    """Has the round this `panel_gave_up` hold is about been overtaken? (#712)"""
    from . import autoreview

    if int(payload.get("assumption_id") or 0) and assumption is not None:
        if str(assumption.get("status") or "") != "pending":
            return True
    if str(payload.get("code") or "") == autoreview.HELD_STATUS:
        return status in autoreview.REVIEW_PASS_STATUSES
    if str(payload.get("code") or "") != autoreview.HELD_PANEL_GAVE_UP:
        return False
    if not latest_round or str(latest_round.get("outcome") or "").lower() != "escalated":
        return True
    held_round = int(payload.get("round") or 0)
    return bool(held_round) and held_round != int(latest_round.get("round") or 0)


def _stale_panel_hold(store: ProjectStore, wo_id: str, *, status: str | None = None):
    """`_panel_hold_is_stale` bound to this work order, reading its rows LAZILY and ONCE"""
    from . import autoreview

    cache: dict[str, Any] = {}
    rows: dict[int, dict[str, Any] | None] = {}
    codes = (autoreview.HELD_PANEL_GAVE_UP, autoreview.HELD_STATUS)

    def stale(kind: str, payload: dict[str, Any]) -> bool:
        if kind != "autoreview_held":
            return False
        code = str(payload.get("code") or "")
        aid = int(payload.get("assumption_id") or 0)
        if aid and aid not in rows:
            rows[aid] = store.get_assumption(aid)
        row = rows.get(aid)
        # The `codes` gate is the ROUND/STATUS clauses' alone: spec §3.3 of...
        if code not in codes:
            return _panel_hold_is_stale(None, payload, "", row)
        if "status" not in cache:
            cache["status"] = (status if status is not None
                              else str(store.get_work_order(wo_id)["status"] or ""))
        if code == autoreview.HELD_STATUS:
            return _panel_hold_is_stale(None, payload, cache["status"], row)
        if "round" not in cache:
            cache["round"] = store.latest_validation_round(wo_id=wo_id)
        return _panel_hold_is_stale(cache["round"], payload, cache["status"], row)

    return stale


def _unreachable_asks():
    """`question_id -> {"question_id", "hint"} | None` — the asks whose question is DEAD"""
    from .neo_store import UNREACHABLE_PREFIX, NeoStore

    cache: dict[int, dict[str, Any] | None] = {}

    def dead(question_id: Any) -> dict[str, Any] | None:
        qid = int(question_id or 0)
        if not qid:
            return None
        if qid not in cache:
            cache[qid] = None
            try:
                neo = NeoStore()
                try:
                    q = neo.get(qid)
                finally:
                    neo.close()
            except Exception:  # noqa: BLE001 — see docstring: never take a surface down
                q = None
            if q and str(q["status"] or "") == "failed":
                hint = str(q["answer_reason"] or "")
                if hint.startswith(UNREACHABLE_PREFIX):
                    hint = hint[len(UNREACHABLE_PREFIX):]
                cache[qid] = {"question_id": qid, "hint": " ".join(hint.split())[:160]}
        return cache[qid]

    return dead


def assumptions_with_rulings(store: ProjectStore, wo_id: str) -> list[dict[str, Any]]:
    """`all_assumptions`, each row carrying what the OS DID with it under `os_ruling` (#712)"""
    from . import db

    newest: dict[int, dict[str, Any]] = {}
    # WHY AN OBJECTION WAS TAKEN BACK
    withdrawn_reason: dict[int, str] = {}
    stale = _stale_panel_hold(store, wo_id)
    for kind in AUTOREVIEW_EVENTS:
        for event in store.events_of_kind(wo_id, kind):
            payload = db.from_json(event["payload"], {})
            aid = int(payload.get("assumption_id") or 0)
            if not aid:
                continue
            if kind == "autoreview_objection_withdrawn":
                # The LAST one per assumption, `events_of_kind` being chronological
                withdrawn_reason[aid] = str(payload.get("reason") or "")
            # A hold about a round a later one overtook is not a ruling about now: it is `continue`d
            if stale(kind, payload):
                continue
            # Payload first, so `kind` and `ts` are this function's own answers — `autoreview_state`'s note
            candidate = {**payload, "kind": kind, "ts": float(event["ts"])}
            prev = newest.get(aid)
            if prev is None or ((candidate["ts"], _RULING_RANK[kind])
                                > (prev["ts"], _RULING_RANK[prev["kind"]])):
                newest[aid] = candidate
    # Only the rows whose surviving ruling IS an ask can carry the fact, so only those are asked about
    gone = _unreachable_asks()
    for ruling in newest.values():
        if ruling["kind"] != "autoreview_asked":
            continue
        unreachable = gone(ruling.get("neo_question_id"))
        if unreachable:
            ruling["unreachable"] = unreachable
    base = store.all_assumptions(wo_id)
    # The net the user called wrong, read by the stakes corpus builder (io-51469add)
    alarms = {int(p.get("assumption_id") or 0): str(p.get("marker") or p.get("net") or "")
              for p in (db.from_json(e["payload"], {}) for e in
                        store.events_of_kind(wo_id, STAKES_FALSE_ALARM_EVENT))}
    rows = [{**a, "os_ruling": newest.get(int(a.get("id") or 0)),
             "stakes_false_alarm": alarms.get(int(a.get("id") or 0), ""),
             # Both derivations read the carrier and the timeline
             "objection_undeliverable": (bool(a.get("objection_envelope_id"))
                                         and invariants.objection_undeliverable(store, a)),
             "objection_response": (objection_response(store, a, rows=base)
                                    if a.get("objection_delivered_ts") else None),
             # Asked only of rows that WERE withdrawn, `objection_response`'s discipline
             "objection_withdrawn_reason": (
                 withdrawn_reason.get(int(a.get("id") or 0), "")
                 if a.get("objection_withdrawn_ts") else "")}
            for a in base]
    # Children of this plan that have already landed, for the rows that are still the user's
    over = (_overtaken(store, wo_id)
            if any(str(a.get("status") or "") == "pending" for a in rows) else None)
    return [{**a, "overtaken": over if str(a.get("status") or "") == "pending" else None}
            for a in rows]


def _overtaken(store: ProjectStore, wo_id: str) -> dict[str, int] | None:
    """`{"merged": n, "of": m}` when children of the plan this planner produced have already landed"""
    fo = store.feature_order_for_planner(wo_id)
    if fo is None or fo.get("plan_wo_id") != wo_id:
        return None
    children = store.feature_children(fo["id"], kinds=WORKER_CHILDREN)
    merged = sum(1 for c in children if store.events_of_kind(c["id"], "pr_merged"))
    return {"merged": merged, "of": len(children)} if merged else None


#: The acts that count as THE WORKER ANSWERING an objection, and the verb each is rendered with
_WORKER_ACT_VERB = {"assumption": "the worker recorded another assumption",
                    "message": "the worker replied",
                    "question_asked": "the worker asked Neo",
                    "finished": "the worker finished",
                    "abandoned": "the worker abandoned the order"}


def objection_response(store: ProjectStore, a: dict[str, Any], *,
                       rows: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """THE FIRST THING THE WORKER DID after the objection reached it, or None"""
    from . import db

    delivered = a.get("objection_delivered_ts")
    if not delivered:
        return None
    delivered, wo_id = float(delivered), str(a.get("wo_id") or "")
    acts: list[dict[str, Any]] = []
    for other in (rows if rows is not None else store.all_assumptions(wo_id)):
        if int(other.get("id") or 0) != int(a.get("id") or 0) \
                and float(other.get("ts") or 0.0) > delivered:
            acts.append({"kind": "assumption", "ts": float(other["ts"]),
                         "detail": str(other.get("content") or "")})
    for msg in store.agent_replies(wo_id):
        if float(msg.get("ts") or 0.0) > delivered:
            acts.append({"kind": "message", "ts": float(msg["ts"]),
                         "detail": str(msg.get("content") or "")})
    for kind in ("question_asked", "finished", "abandoned"):
        for event in store.events_of_kind(wo_id, kind):
            if float(event["ts"]) <= delivered:
                continue
            payload = db.from_json(event["payload"], {})
            detail = str(payload.get("question") or payload.get("summary")
                         or payload.get("reason") or "")
            question = payload.get("neo_question_id")
            acts.append({"kind": kind, "ts": float(event["ts"]), "detail": detail,
                         **({"neo_question_id": question} if question else {})})
    if not acts:
        return None
    # THE FIRST act, not the last: the question is what the worker did about the objection
    return min(acts, key=lambda act: act["ts"])


#: The shortest verbatim run of an objection's reason that counts as quoting it (io-ce566253)
OBJECTION_QUOTE_MIN_CHARS = 40


def stamp_objection_delivery(store: ProjectStore, msg_id: int) -> None:
    """An objection that actually went out is DELIVERED, stamped here and nowhere"""
    envelope = store.envelope_for_message(int(msg_id))
    if envelope is None:
        _stamp_late_objection_delivery(store, msg_id)
        return
    if envelope["kind"] != "assumption_objection":
        return
    a = store.assumption_for_envelope(int(envelope["id"]))
    if a is not None and a.get("objection_delivered_ts") is None:
        store.mark_objection_delivered(int(a["id"]))


def _stamp_late_objection_delivery(store: ProjectStore, msg_id: int) -> None:
    """An ordinary message that QUOTES an undelivered objection delivered it, late (io-ce566253)"""
    msg = store.get_message(int(msg_id))
    if not msg or msg.get("direction") != "user_to_agent":
        return
    for a in store.undelivered_objections(str(msg["wo_id"])):
        if objection_quoted_in(str(msg.get("content") or ""), a):
            store.mark_objection_delivered(int(a["id"]), late_msg_id=int(msg_id))


def objection_quoted_in(content: str, a: dict[str, Any]) -> bool:
    """Does `content` carry this objection — half its reason verbatim, case and spacing aside?"""
    import difflib

    def norm(text: str) -> str:
        return " ".join(text.lower().split())

    reason, body = norm(str(a.get("provisional_reason") or "")), norm(content)
    if len(reason) < OBJECTION_QUOTE_MIN_CHARS or not body:
        return False
    run = difflib.SequenceMatcher(None, reason, body, autojunk=False).find_longest_match(
        0, len(reason), 0, len(body)).size
    return run >= max(OBJECTION_QUOTE_MIN_CHARS, len(reason) // 2)


def objection_response_line(a: dict[str, Any]) -> str:
    """WHAT THE WORKER DID about the objection, or that it still owes an answer"""
    delivered = a.get("objection_delivered_ts")
    if not delivered:
        return ""
    act = a.get("objection_response") or None
    if not act:
        return f"waiting on the worker since {_stamp(delivered)}"
    verb = _WORKER_ACT_VERB.get(str(act.get("kind") or ""), "the worker acted")
    question = act.get("neo_question_id")
    detail = " ".join(str(act.get("detail") or "").split())
    if len(detail) > 120:
        detail = detail[:117] + "…"
    return (f"{verb}{f' (question {question})' if question else ''} "
            f"at {_stamp(act['ts'])}{' — ' + detail if detail else ''}")


def assumption_ruling_line(a: dict[str, Any]) -> str:
    """WHAT THE OS DID with this assumption, in one line, or `''`"""
    if str(a.get("status") or "") != "pending":
        return str(a.get("decided_reason") or "").strip()
    ruling = a.get("os_ruling") or {}
    kind, reason = str(ruling.get("kind") or ""), str(ruling.get("reason") or "").strip()
    question = ruling.get("neo_question_id")
    if kind == "autoreview_escalated" and ruling.get("re_asked"):
        return (f"The OS dropped Neo's ruling (question {question}) and will ask again"
                f"{' — ' + reason if reason else ''}")
    if kind == "autoreview_escalated":
        asked = f" (question {question})" if question else ""
        return f"Neo escalated{asked}{': ' + reason if reason else ''}"
    if kind == "autoreview_held":
        held = re.sub(r"^assumption #\d+ ", "", reason)
        return f"Held by the OS — {held}" if held else "Held by the OS"
    if kind == "autoreview_asked":
        # A dead question is not a ruling on its way
        gone = ruling.get("unreachable") or {}
        if gone:
            what = " to confirm its early reading" if ruling.get("confirm") else ""
            tail = f" — last error: {gone['hint']}" if gone.get("hint") else ""
            return (f"Neo could not be reached (question {gone['question_id']}) — nobody "
                    f"judged this{what}; you decide it: `jarvis neo answer "
                    f"{gone['question_id']} \"…\"` then `jarvis wo review "
                    f"{a.get('wo_id')}`{tail}")
        return f"Asked Neo (question {question}), awaiting ruling"
    if kind == "autoreview_unconfirmed":
        # The early reading did not survive the diff
        asked = f" (question {question})" if question else ""
        return (f"Neo did not confirm its early reading{asked}"
                f"{': ' + reason if reason else ''}")
    # Every other early-pass kind is already on the ROW
    return ""


def overtaken_line(a: dict[str, Any]) -> str:
    """CHILDREN OF THIS PLAN THAT ALREADY MERGED, in one line, or `''`"""
    over = a.get("overtaken") or {}
    if not over:
        return ""
    return (f"{over['merged']} of {over['of']} children have already merged — rejecting "
            f"this now means a follow-up fix, not an unwind")


def _stamp(ts: Any) -> str:
    """A timestamp a person can read"""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))


def provisional_line(a: dict[str, Any]) -> str:
    """NEO'S EARLY READING of this assumption, in one line, or `''`"""
    verdict = str(a.get("provisional_verdict") or "")
    if not verdict:
        return ""
    reason = str(a.get("provisional_reason") or "").strip()
    model = a.get("provisional_model") or "model not recorded"
    when = f" at {_stamp(a['provisional_ts'])}" if a.get("provisional_ts") else ""
    verb = ("provisionally accepted by the OS" if verdict == "accept"
            else "objected to by the OS")
    return (f"{verb} (Neo, {model}){when}"
            f"{' — ' + reason if reason else ''}")


def objection_line(a: dict[str, Any]) -> str:
    """THE OBJECTION SENT TO THE WORKER: how it travelled, and how it ended"""
    if not a.get("objection_envelope_id"):
        return ""
    transport = str(a.get("objection_transport") or "") or "queue"
    sent = f" at {_stamp(a['objection_sent_ts'])}" if a.get("objection_sent_ts") else ""
    head = f"objection sent to the worker over the {transport}{sent}"
    if a.get("objection_delivered_ts") and a.get("objection_late_msg_id"):
        return (f"{head}, delivered late at {_stamp(a['objection_delivered_ts'])} "
                f"in message {a['objection_late_msg_id']}")
    if a.get("objection_delivered_ts"):
        return f"{head}, delivered at {_stamp(a['objection_delivered_ts'])}"
    if a.get("objection_withdrawn_ts"):
        # OFF THE ROW, never hard-coded: there are two reasons since issue 1112 and a renderer that knows...
        why = (str(a.get("objection_withdrawn_reason") or "")
               or OBJECTION_WITHDRAWN_REASON)
        return f"{head}, withdrawn at {_stamp(a['objection_withdrawn_ts'])} — {why}"
    if a.get("objection_undeliverable"):
        return f"{head}, UNDELIVERABLE — the worker was never told"
    return f"{head}, in flight"


def assumption_line(a: dict[str, Any]) -> str:
    """One assumption on one line, for `jarvis wo show`"""
    n, status = a.get("n"), str(a.get("status") or "")
    content = str(a.get("content") or "")
    if status == "pending":
        parts = [p for p in (assumption_ruling_line(a), provisional_line(a),
                             objection_line(a), objection_response_line(a),
                             overtaken_line(a)) if p]
        return (f"#{n} pending your review: {content}"
                f"{' — ' + '; '.join(parts) if parts else ''}")
    reason = str(a.get("decided_reason") or "").strip()
    # The early reading stays on a SETTLED row too: what the OS thought while the work ran and what it...
    tail = "; ".join(p for p in (provisional_line(a), objection_line(a),
                                 objection_response_line(a)) if p)
    return (f"#{n} {status} by {assumption_decider(a)}"
            f"{' — ' + reason if reason else ''}: {content}"
            f"{' — ' + tail if tail else ''}")


def _autoreview_line(state: dict[str, Any]) -> str:
    """One line"""
    kind = state["kind"]
    n = state.get("n")
    # One shared suffix: both "left with you" branches make the same claim...
    since = (f"; since {state['resolved_status']} by {state['resolved_decider']}"
             if state.get("resolved_status") else "")
    if kind == "autoreview_accepted":
        return (f"assumption #{n} accepted by the OS (Neo, "
                f"{state.get('model') or 'model not recorded'}) — "
                f"{state.get('reason') or 'no reason recorded'}")
    if kind == "autoreview_escalated" and state.get("re_asked") and not since:
        return (f"assumption #{n} goes back to Neo — the OS dropped its ruling: "
                f"{state.get('reason') or 'no reason recorded'}")
    if kind == "autoreview_escalated":
        return (f"assumption #{n} left with you — "
                f"{state.get('reason') or 'no reason recorded'}{since}")
    if kind == "autoreview_asked":
        # `left with you` deliberately: the same claim as the two `_AUTOREVIEW_OWED_BY_USER` branches
        gone = state.get("unreachable") or {}
        if gone:
            return (f"assumption #{n} left with you — Neo could not be reached "
                    f"(question {gone['question_id']}); nobody judged it")
        return f"assumption #{n} is with Neo (question {state.get('neo_question_id')})"
    # The early pass's five
    if kind == "autoreview_provisional":
        return (f"assumption #{n} provisionally {state.get('verdict') or 'judged'} by "
                f"the OS while the worker runs — settles nothing")
    if kind == "autoreview_objected":
        return (f"the OS objected to assumption #{n} and told the worker — "
                f"{state.get('reason') or 'no reason recorded'}")
    if kind == "autoreview_objection_withdrawn":
        return (f"objection to assumption #{n} withdrawn undelivered — "
                f"{state.get('reason') or 'no reason recorded'}")
    if kind == "autoreview_confirmed":
        return (f"assumption #{n} confirmed against the result and accepted by the OS "
                f"(Neo, {state.get('model') or 'model not recorded'}) — "
                f"{state.get('reason') or 'no reason recorded'}")
    if kind == "autoreview_unconfirmed":
        return (f"assumption #{n} left with you — the OS did not confirm its early "
                f"reading: {state.get('reason') or 'no reason recorded'}{since}")
    return f"held — {state.get('reason') or 'no reason recorded'}"


def _automerge_line(state: dict[str, Any]) -> str:
    """One line, in the words the spec's §5.4 chose"""
    kind = state["kind"]
    if kind == "automerge_merged":
        return (f"merged by the OS at {str(state.get('head_sha') or '')[:10]}, under "
                f"gate request {state.get('approval_id')} "
                f"(round {state.get('round')})")
    if kind == "automerge_decided":
        return (f"{state.get('decision')} by {state.get('by')} — "
                f"{state.get('reason') or 'no reason given'}")
    if kind == "automerge_proposed":
        return (f"waiting on gate request {state.get('approval_id')} to merge "
                f"{str(state.get('head_sha') or '')[:10]}")
    if kind == "automerge_failed":
        return f"the merge failed: {state.get('reason') or 'no reason recorded'}"
    # A hold `automerge_state` marked stale
    if state.get("stale_because") == "status":
        return f"not parked for merge: {state.get('stale_status') or 'in no status'}"
    if state.get("stale_because") == "round":
        return (f"round {state.get('round')}'s hold is out of date — round "
                f"{state.get('stale_round')} is the current round")
    # THIS ONE DOES QUOTE THE REASON: past tense beside "was held" it is the history the user asked for
    if state.get("stale_because") == "rejudging":
        reason = state.get("reason") or "no reason recorded"
        if round_n := int(state.get("stale_round") or 0):
            return f"re-judging in round {round_n} (was held: {reason})"
        return f"re-judging (was held: {reason})"
    return f"held — {state.get('reason') or 'no reason recorded'}"


def validation_detail(store: ProjectStore, *, wo_id: str | None = None,
                      fo_id: str | None = None) -> dict[str, Any]:
    """THE DELIBERATION on one unit, in full: every round with every seat"""
    # `validation_rounds` is a DIFFERENT projection
    filed = filed_follow_ups(store, wo_id=wo_id, fo_id=fo_id)
    rounds = [{**rnd, "opinions": store.validation_opinions(rnd["id"]),
               "follow_ups": filed.get(int(rnd["id"]), NO_FOLLOW_UPS)}
              for rnd in store.validation_rounds(wo_id=wo_id, fo_id=fo_id)]
    envelopes = store.envelopes(subject_wo_id=wo_id, subject_fo_id=fo_id)
    return {"rounds": rounds, "envelopes": envelopes,
            # Pulled out rather than left for every reader to filter: an undeliverable envelope is a failure
            "undeliverable": [e for e in envelopes if e["state"] == "undeliverable"]}


def validation_view(unit_id: str, project_name: str | None = None) -> dict[str, Any]:
    """`validation_detail` for a unit named on the command line, either kind"""
    if is_feature_order_id(unit_id):
        name, path, row = find_feature_order(unit_id, project_name)
        subject: dict[str, str | None] = {"fo_id": unit_id}
        unit = "feature"
    else:
        name, path, row = find_work_order(unit_id, project_name)
        subject = {"wo_id": unit_id}
        unit = "work_order"
    store = ProjectStore(path)
    try:
        detail = validation_detail(store, **subject)  # type: ignore[arg-type]
        # Spec 2026-10-14-an-escalated-round-says-why-and-what-to-run-next §5
        owed = review_state(store, row) if unit == "work_order" else None
    finally:
        store.close()
    return {"project": name, "unit": unit, "id": unit_id,
            "title": row["title"], "status": row["status"], **detail,
            **({"review": owed} if owed else {})}


def current_config_version() -> str | None:
    """The id of the configuration in force, or None when the ledger holds nothing"""
    central = CentralStore()
    try:
        return (central.head_config_version() or {}).get("id")
    finally:
        central.close()


def config_version_line(version_id: str | None) -> str:
    """One configuration stamp, as a person reads it: the id and how far behind it is"""
    if not version_id:
        return "not recorded"
    central = CentralStore()
    try:
        since = central.config_versions_since(version_id)
    finally:
        central.close()
    if since is None:
        return f"{version_id} (no longer in the ledger)"
    return f"{version_id} ({since} versions since)" if since else version_id


def concision_config(project: str | None = None) -> Any:
    """The concision settings in force for `project` — or the OS's — or None"""
    try:
        catalog = resolve_catalog()
        if project is None:
            return catalog.os.concision
        return catalog.project(project).concision
    except (OpsError, CatalogError, OSError, ValueError):
        return None


def _concision_cap(project: str | None, field: str, default: int) -> int:
    """One per-project character cap, falling back to the fleet default (docs/superpowers/specs/2026-10-21-questions-to-neo-are-atomic.md §2)"""
    cfg = concision_config(project)
    return default if cfg is None else int(getattr(cfg, field, default))


def validation_config(project: str | None = None) -> Any:
    """The validation settings in force for `project` — or the OS's — or None"""
    try:
        catalog = resolve_catalog()
        if project is None:
            return catalog.os.validation
        return catalog.project(project).validation
    except (OpsError, CatalogError, OSError, ValueError):
        return None


def land_finished(store: ProjectStore, wo: dict[str, Any],
                  pr_url: str | None = None) -> str:
    """Where a work order that has genuinely finished lands, and the backlog item it closes on the way (#232)"""
    wo_id = wo["id"]
    pr_url = pr_url or (str(wo.get("pr_url") or "")
                        if routes_on_pull_request(store, wo) else "") or None
    if not pr_url and not store.work_abandoned(wo_id):
        stranding = unlanded_work(store, wo)
        if stranding.produced:
            # The one parker: every route to `completed` passes here (docs/superpowers/specs/2026-10-12-a-pushed-branch-with-no-pull-request-is-a-state.md §2)
            if stranding.pushed and kind_contract(wo).lands_code:
                return park_pr_pending(store, wo, stranding)
            return park_unlanded(store, wo, stranding)
    status = "waiting_pr_merge" if pr_url else "completed"
    # Spec §C of docs/superpowers/specs/2026-10-14-a-message-must-not-drop-an-owed-review.md
    store.set_status(wo_id, status, trigger="landed")
    store.clear_attention(wo_id)
    if wo.get("backlog_id") and status == "completed":
        central = CentralStore()
        try:
            central.mark_backlog(wo["backlog_id"], "done")
        finally:
            central.close()
    return status


#: The `pr_url_recorded` sources that route the merge queue; `gate` and `plan_submit` stay inert
DECLARING_SOURCES = ("user", "auto")


def declared_pull_request(store: ProjectStore, wo: dict[str, Any]) -> str:
    """The pull request the SUBMITTER declared (docs/superpowers/specs/2026-10-12-a-pushed-branch-with-no-pull-request-is-a-state.md §8.1)"""
    # Newest first; read order breaks a timestamp tie, the later row winning
    events = [(float(e["ts"]), seq, kind, db.from_json(e["payload"], {}))
              for seq, (kind, e) in enumerate(
                  (kind, e) for kind in ("finished", "pr_url_recorded")
                  for e in store.events_of_kind(wo["id"], kind))]
    for _ts, _seq, kind, payload in sorted(events, key=lambda e: e[:2], reverse=True):
        declared = str(payload.get("pr_url") or "")
        if not declared:
            continue
        if kind == "finished" or str(payload.get("source") or "") in DECLARING_SOURCES:
            return declared
    return ""


def routes_on_pull_request(store: ProjectStore, wo: dict[str, Any]) -> bool:
    """Whether `wo['pr_url']` may move this work order — the merge queue's one test (#742)"""
    if not wo.get("pr_url"):
        return False
    if not store.events_of_kind(wo["id"], "pr_url_recorded"):
        return True
    return bool(declared_pull_request(store, wo))


def unlanded_work(store: ProjectStore, wo: dict[str, Any],
                  pr_url: str = "") -> landing.Authored:
    """What settling this work order right now would strand (#232)"""
    from . import landing

    recorded = store.get_work_order(wo["id"])
    if pr_url or recorded.get("pr_url"):
        return landing.Authored()
    return authorship(store, wo)


def authorship(store: ProjectStore, wo: dict[str, Any]) -> landing.Authored:
    """What this work order's worktree has produced, asked WITHOUT the pull-request rule"""
    from . import landing

    recorded = store.get_work_order(wo["id"])
    return landing.authored(landing.worktree_of(store.project_path, recorded),
                            submission_artifacts(recorded))


def unmerged_pull_request(store: ProjectStore, wo_id: str) -> str:
    """This order's recorded pull request, unless the record says it merged"""
    from . import landing

    pr_url = str(store.get_work_order(wo_id).get("pr_url") or "")
    if not pr_url or store.events_of_kind(wo_id, "pr_merged"):
        return ""
    seen = store.events_of_kind(wo_id, "landing_seen")
    if seen and not landing.from_record(
            wo_id, db.from_json(seen[-1]["payload"], {})).unsettled:
        return ""
    return pr_url


def park_unlanded(store: ProjectStore, wo: dict[str, Any],
                  work: landing.Authored) -> str:
    """Hold a work order that would otherwise complete over unlanded code, and SAY SO (#232)"""
    if store.work_unlanded_open(wo["id"]):
        store.set_status(wo["id"], "needs_review", trigger="unlanded_repark")
        return "needs_review"
    store.add_event(wo["id"], "work_unlanded", {**work.record(), "was": wo["status"]})
    store.set_status(wo["id"], "needs_review")
    store.flag_attention(wo["id"], UNLANDED_BLOCKER)
    return "needs_review"


def park_pr_pending(store: ProjectStore, wo: dict[str, Any],
                    work: landing.Authored) -> str:
    """Hold an order whose branch is pushed with no pull request (docs/superpowers/specs/2026-10-12-a-pushed-branch-with-no-pull-request-is-a-state.md §2)"""
    if store.pr_pending_open(wo["id"]):
        store.set_status(wo["id"], "needs_review", trigger="pr_pending_repark")
        return "needs_review"
    park = {**work.record(), "was": wo["status"]}
    store.add_event(wo["id"], PR_PENDING_EVENT, park)
    store.set_status(wo["id"], "needs_review")
    store.flag_attention(wo["id"], pr_pending_blocker(wo["id"], park))
    return "needs_review"


def defer_red_release(store: ProjectStore, project_name: str,
                      wo: dict[str, Any]) -> str | None:
    """Hold a release order that delivered NO release while the base is unbuildable (docs/superpowers/specs/2026-09-29-a-release-blocked-by-a-red-main-retries-itself.md §1)"""
    from .central_store import BASE_HEALTH_FRESH_SECONDS
    from .daemon import Daemon

    wo_id = str(wo["id"])
    if not release.is_release_order(wo) or _release_effects(store, wo_id):
        return None
    fact = _stored_base_health(project_name)
    checked = fact.get("checked_at")
    # `db.now()` for both clocks in this function
    fresh = (isinstance(checked, (int, float))
             and db.now() - float(checked) <= BASE_HEALTH_FRESH_SECONDS)
    if fresh and not fact.get("red"):
        return None
    first = first_red_hold(store, wo_id)
    if first is not None and db.now() - first >= Daemon.RED_PARK_AFTER_SECONDS:
        return park_red_release(store, wo_id, fact)
    # `hold_dispatch` writes `retry_after` ONLY: `dispatch_attempts` is `release_dispatch_claim`'s...
    store.set_status(wo_id, "pending", trigger="release_red_base")
    store.hold_dispatch(wo_id, until=db.now() + Daemon.RED_HOLD_SECONDS)
    said = _red_base_said(fact)
    # One event per BROKEN COMMIT, deduped on the head sha exactly as `_say_base_is_red` does: a code per...
    if not any(db.from_json(e["payload"], {}).get("head_sha") == said["head_sha"]
               for e in store.events_of_kind(wo_id, RED_DEFER_EVENT)):
        store.add_event(wo_id, RED_DEFER_EVENT, {**said, "was": wo["status"]})
    return "pending"


def _stored_base_health(project_name: str) -> dict[str, Any]:
    """The stored reading, or `{}` — which re-parks, exactly as a red one does"""
    from .central_store import CentralStore

    try:
        central = CentralStore()
    except Exception:  # noqa: BLE001 — an unreadable fact is not a green base
        return {}
    try:
        return central.base_health(project_name)
    except Exception:  # noqa: BLE001 — ditto
        return {}
    finally:
        central.close()


def _base_is_red(spec: Any, now: float, fact: dict[str, Any] | None = None) -> bool:
    """Is the OS itself holding this project's merges because its base is red? (docs/superpowers/specs/2026-10-15-a-merge-held-by-a-red-base-is-not-stuck.md)"""
    from .central_store import BASE_HEALTH_FRESH_SECONDS

    if not (spec.validation.enabled and spec.validation.auto_merge):
        return False
    fact = _stored_base_health(spec.name) if fact is None else fact
    if not fact.get("red"):
        return False
    checked_at = fact.get("checked_at")
    if not isinstance(checked_at, (int, float)):
        return False
    return now - float(checked_at) <= BASE_HEALTH_FRESH_SECONDS


def _base_red_until(fact: dict[str, Any], now: float) -> float:
    """Where an OPEN red episode stops counting: the fact's freshness horizon"""
    from .central_store import BASE_HEALTH_FRESH_SECONDS

    checked_at = fact.get("checked_at")
    if not isinstance(checked_at, (int, float)):
        return 0.0
    return min(now, float(checked_at) + BASE_HEALTH_FRESH_SECONDS)


def _stored_base_red_episodes(project_name: str) -> list[list[float | None]]:
    """This project's red-base episodes, or `[]` — `_stored_base_health`'s tolerance"""
    from .central_store import CentralStore

    try:
        central = CentralStore()
    except Exception:  # noqa: BLE001 — an unreadable list discounts nothing
        return []
    try:
        return central.base_red_episodes(project_name)
    except Exception:  # noqa: BLE001 — ditto
        return []
    finally:
        central.close()


def _red_base_said(fact: dict[str, Any]) -> dict[str, Any]:
    """What the timeline carries about the run that is holding this release"""
    base = str(fact.get("base") or "main")
    head = str(fact.get("head_sha") or "")
    workflow = str(fact.get("workflow") or "ci")
    where = f" at {head[:10]}" if head else ""
    return {"base": base, "head_sha": head, "workflow": workflow,
            "run_url": str(fact.get("run_url") or ""),
            "run_id": fact.get("run_id") or 0,
            "detail": (f"{base} is not buildable{where} ({workflow}) — this release "
                       f"delivered no release and waits for the base to go green")}


def first_red_hold(store: ProjectStore, wo_id: str) -> float | None:
    """When this order was FIRST held on a red base, by either path — §3's one clock (#807)"""
    from .daemon import Daemon

    stamps = [float(e["ts"])
              for kind in (Daemon.RED_HOLD_EVENT, RED_DEFER_EVENT)
              for e in store.events_of_kind(wo_id, kind)]
    return min(stamps) if stamps else None


def park_unshipped_release(store: ProjectStore, wo: dict[str, Any]) -> str | None:
    """A release order that cut NO release asks the user instead of completing (issue #1206)"""
    wo_id = str(wo["id"])
    if not release.is_release_order(wo) or _release_effects(store, wo_id):
        return None
    store.add_event(wo_id, release.NOTHING_SHIPPED_EVENT, {"was": wo["status"]})
    store.set_status(wo_id, "needs_review", trigger="release_shipped_nothing")
    store.flag_attention(
        wo_id, invariants.RELEASE_NOTHING_SHIPPED_BLOCKER.format(wo_id=wo_id))
    return "needs_review"


def park_red_release(store: ProjectStore, wo_id: str, fact: dict[str, Any]) -> str:
    """Past the threshold the release stops waiting and asks the user (§3)"""
    if store.release_red_park_open(wo_id):
        store.set_status(wo_id, "needs_review", trigger="release_red_repark")
        return "needs_review"
    said = _red_base_said(fact)
    store.add_event(wo_id, RED_PARK_EVENT, said)
    store.set_status(wo_id, "needs_review")
    store.flag_attention(wo_id, invariants.release_base_red_blocker(said))
    return "needs_review"


def land_when_cleared(store: ProjectStore, wo: dict[str, Any],
                      pr_url: str | None = None, *,
                      panel_cleared: bool = False,
                      panel_open: bool = False) -> str:
    """THE JOIN: where a finished work order sits, given BOTH gates over it (docs/superpowers/specs/2026-09-13-two-gates-not-a-chain.md §2)"""
    assert not (panel_cleared and panel_open), "the panel is settled or it is not"
    wo_id = wo["id"]
    if invariants.reviewable_assumptions(store, wo):
        store.set_status(wo_id, "needs_review")
        store.flag_attention(wo_id, "assumptions pending review")
        return "needs_review"
    if not refusal_answered(store, wo_id):
        # The flag and the guidance are `review_work_order`'s
        store.set_status(wo_id, "needs_review")
        return "needs_review"
    latest = store.latest_validation_round(wo_id=wo_id) if not panel_cleared else None
    outcome = str((latest or {}).get("outcome") or "")
    if panel_open or outcome in OPEN_VALIDATION_OUTCOMES:
        store.set_status(wo_id, "validating")
        store.clear_attention(wo_id)
        return "validating"
    # `escalated` lands
    return land_finished(store, wo, pr_url)


def refusal_answered(store: ProjectStore, wo_id: str) -> bool:
    """Has the worker delivered again since the user last REFUSED an assumption? (docs/superpowers/specs/2026-10-08-a-passed-forced-round-answers-a-refusal.md)"""
    refusals = [e for e in store.events_of_kind(wo_id, "reviewed")
                if not db.from_json(e["payload"], {}).get("accepted", True)]
    if not refusals:
        return True
    cut = float(refusals[-1]["ts"])
    delivered = store.events_of_kind(wo_id, "finished")
    if delivered and float(delivered[-1]["ts"]) > cut:
        return True
    # One read of the rows, like `validated_head`'s one-read rule: the guard needs the prior counted row...
    counted = [r for r in store.validation_rounds(wo_id=wo_id)
               if str(r["outcome"] or "") in COUNTED_VALIDATION_OUTCOMES]
    if not counted:
        return False
    newest = counted[-1]
    # `validation_rounds.ts` is the round's OPEN time and nothing writes a settle one
    if (str(newest["outcome"] or "") != "passed"
            or str(newest.get("uncounted_cause") or "") != USER_REWORK_CAUSE
            or float(newest["ts"] or 0) <= cut):
        return False
    forced_head = ProjectStore.validated_head(newest) or ""
    before = [r for r in counted[:-1] if float(r["ts"] or 0) <= cut]
    prior = before[-1] if before else None
    prior_head = ((ProjectStore.validated_head(prior) or str(prior["head_sha"] or ""))
                  if prior is not None else "")
    # Spec §The head guard on (b): an unmoved head is the refused decision landing by the back door
    return not (forced_head and forced_head == prior_head)


def user_rework_pending(store: ProjectStore, wo_id: str) -> bool:
    """Is this work order carrying a rework the USER asked for and nobody has judged?"""
    refusals = [e for e in store.events_of_kind(wo_id, "reviewed")
                if not db.from_json(e["payload"], {}).get("accepted", True)]
    if not refusals:
        return False
    judged = store.last_judged_round(wo_id=wo_id)
    return judged is None or float(refusals[-1]["ts"]) > float(judged["ts"] or 0)


#: HOW MANY TIMES RUNNING a submitter may be sent back without the panel before the OS stops trying... (docs/superpowers/specs/2026-09-22-a-round-must-answer-the-list.md §6.)
BOUNCE_LIMIT = 2

#: WHAT A BOUNCED SUBMITTER IS TOLD
BOUNCE_FEEDBACK = """REVIEW FEEDBACK (no round was spent)
Round {n} asked you to change {paths}, and this submission changes none of them. It was
not sent to the panel: a resubmission that answers nothing on the list costs a review
round and moves the work order no further.

Go back to round {n}'s feedback and address it. If you believe one of those points is
already answered, or should not be, say which and why in `--evidence` and change the
file it is about anyway — the review cannot read an argument you did not make in the
diff. Then run `jarvis wo finish {wo_id} --summary "..." --evidence "..."` again."""

#: WHAT A FIX ORDER REFUSED BY THE PRE-CHECK IS TOLD (§7.2 of... (docs/superpowers/specs/2026-09-27-self-evolution.md)
PRECHECK_FEEDBACK = """REVIEW FEEDBACK (no round was spent)
{reason}

This submission was not sent to the panel. The seats judge the QUALITY of a rule —
whether the condition matches the real symptom, whether the remedy is the right
primitive — and none of that can be judged until the rule exists and has been shown to
fire on the order it is proved against. Nothing was registered for you: a fix order that
has not registered its rule has not finished its job.

Fix the above, then run `jarvis wo finish {wo_id} --summary "..." --evidence "..."`
again."""

#: The same refusal at the DAEMON site
PRECHECK_SPENT_NOTE = (
    "\n\nThis round was opened before the check could run, so it HAS been spent.")

#: The give-up when that has happened `BOUNCE_LIMIT` times running
BOUNCE_EXHAUSTED = (
    "this work order has been sent back {n} times running for changing nothing round "
    "{round} asked about ({paths}), and it is still changing none of them. Nobody has "
    "judged the latest submission: decide whether the review's list is right before "
    "spending another round on it.")


def escalate_validation_round(store: ProjectStore, wo: dict[str, Any], round_id: int,
                              n: int, reason: str, *, cause: str) -> None:
    """Give up on this unit and ask the user (docs/superpowers/specs/2026-10-14-an-escalated-round-says-why-and-what-to-run-next.md §2)"""
    from .daemon import escalation_body
    from .invariants import VALIDATION_STUCK_BLOCKER

    wo_id = wo["id"]
    store.settle_validation_round(
        round_id, "escalated",
        {"round": n, "round_id": round_id, "reason": reason, "cause": cause},
        wo_id=wo_id, reason=reason)
    store.set_status(wo_id, "needs_review")
    store.flag_attention(wo_id, VALIDATION_STUCK_BLOCKER)
    store.add_notification(
        # Spec 2026-10-14-an-escalated-round-says-why-and-what-to-run-next §3
        title=ESCALATION_TITLE.format(
            unit=wo_id, n=n, verb=ESCALATION_VERB[ESCALATION_FAMILY[cause]]),
        body=escalation_body(reason), level="warning", wo_id=wo_id,
        source="validation", paired=True,
    )


def escalation_cause(store: ProjectStore, *, wo_id: str, round_id: int) -> str:
    """WHICH CONDITION escalated this round, or `ESCALATION_CAUSE_UNRECORDED` (docs/superpowers/specs/2026-10-14-an-escalated-round-says-why-and-what-to-run-next.md §2.)"""
    for event in reversed(store.events_of_kind(wo_id, "validation_escalated")):
        payload = db.from_json(event["payload"], {})
        if int(payload.get("round_id") or 0) == int(round_id):
            return str(payload.get("cause") or ESCALATION_CAUSE_UNRECORDED)
    return ESCALATION_CAUSE_UNRECORDED


def _worktree_file_test(project_path: Path,
                        wo: Mapping[str, Any]) -> Callable[[str], bool] | None:
    """`exists` for `validation.unanswered_submission`, or None when no worktree resolves (docs/superpowers/specs/2026-09-22-a-round-must-answer-the-list.md §5)"""
    from . import evidence as evidence_mod
    from . import worker_session

    # type: ignore — `ProjectRef` carries the one attribute that helper reads.
    worktree = worker_session.worktree_path(
        evidence_mod.ProjectRef(project_path), dict(wo))  # type: ignore[arg-type]
    if worktree is None:
        return None

    def present(path: str) -> bool:
        candidate = Path(path)
        if candidate.is_absolute() or ".." in candidate.parts:
            return False
        try:
            return (worktree / candidate).is_file()
        except (OSError, ValueError):
            return False

    return present


def unanswered_paths(store: ProjectStore, wo_id: str, packet: Any, *,
                     project_path: Path,
                     wo: Mapping[str, Any],
                     ) -> tuple[dict[str, Any], tuple[str, ...]] | None:
    """`(the round that asked, the paths it asked about)` when this submission answers none of them"""
    from . import validation

    previous = store.last_judged_round(wo_id=wo_id)
    if previous is None:
        return None
    raised: list[dict[str, str]] = []
    for op in store.validation_opinions(int(previous["id"])):
        raised += validation.blockers(validation.findings(op))
    cited = validation.unanswered_submission(
        previous, raised, ProjectStore.validation_file_shas(previous),
        dict(packet.file_shas), exists=_worktree_file_test(project_path, wo))
    return (previous, cited) if cited is not None else None


def consecutive_bounces(store: ProjectStore, wo_id: str, after_round: int) -> int:
    """How many times running this submitter has already been bounced off `after_round`"""
    return sum(1 for e in store.events_of_kind(wo_id, "validation_bounced")
               if int(db.from_json(e["payload"], {}).get("after_round") or 0)
               == after_round)


def submit_for_validation(store: ProjectStore, project_path: Path, wo: dict[str, Any],
                          *, declared: str, cfg: Any,
                          forced_reason: str = "",
                          uncounted: bool = False,
                          uncounted_cause: str = "") -> dict[str, Any] | None:
    """Open a validation round over what this work order has produced, or bounce it (docs/superpowers/specs/2026-09-15-forcing-a-validation-round.md §4.)"""
    from . import evidence as evidence_mod
    from . import specs

    # THE MECHANICAL GATE, BEFORE THE PACKET AND BEFORE THE ROUND (§7.2 of... (docs/superpowers/specs/2026-09-27-self-evolution.md)
    unproven: list[str] = []
    if not (forced_reason.strip() or uncounted):
        refusal = fix_order_precheck(store, project_path, wo, unproven=unproven)
        if refusal is not None:
            _precheck_refused(store, wo, refusal,
                              store.numbered_validation_rounds(wo_id=wo["id"]) + 1)
            return None

    # A FIX ASKED FOR BY A REBIND IS ITSELF A REBIND (spec 2026-09-27-a-conflict-...
    if not uncounted:
        previous = store.last_judged_round(wo_id=str(wo["id"]))
        if (previous is not None and int(previous["uncounted"] or 0)
                and str(previous["outcome"] or "") == "rejected"
                # REBIND ONLY
                and str(previous["uncounted_cause"] or "") == REBIND_CAUSE):
            uncounted, uncounted_cause = True, REBIND_CAUSE
        elif user_rework_pending(store, str(wo["id"])):
            # The worker's own re-delivery of a rework the USER asked for, through `jarvis wo finish`
            uncounted, uncounted_cause = True, USER_REWORK_CAUSE
    packet = evidence_mod.collect_work_order(
        project_path, wo, declared=declared, diff_chars=cfg.diff_chars,
        spec=specs.spec_of(store, wo), side_effects=side_effects_of(store, str(wo["id"])),
        # The store read is the caller's: `evidence` may not touch a database (spec §4)
        assumptions=store.all_assumptions(wo["id"]),
        unproven=tuple(unproven))
    # The NUMBER, not the budget position: a rebind spends one without spending the other (spec...
    nxt = store.numbered_validation_rounds(wo_id=wo["id"]) + 1
    unanswered = (None if forced_reason.strip()
                  else unanswered_paths(store, str(wo["id"]), packet,
                                        project_path=project_path, wo=wo))
    if unanswered is not None:
        previous, cited = unanswered
        if consecutive_bounces(store, str(wo["id"]),
                               int(previous["round"])) < BOUNCE_LIMIT:
            _bounce(store, wo, previous, cited, nxt)
            return None
        # The ceiling
        round_row = store.open_validation_round(
            wo_id=wo["id"], fingerprint=evidence_mod.fingerprint(packet),
            summary=str(wo.get("result_summary") or ""), evidence=declared,
            pr_url=wo.get("pr_url"), round=nxt,
            config_version=current_config_version())
        store.set_validation_file_shas(int(round_row["id"]), packet.file_shas)
        escalate_validation_round(
            store, wo, int(round_row["id"]), nxt,
            BOUNCE_EXHAUSTED.format(n=BOUNCE_LIMIT, round=int(previous["round"]),
                                    paths=", ".join(cited)),
            cause=ESCALATION_BOUNCE_EXHAUSTED)
        # RE-READ, because the row above is the one that was opened and the caller has to be able to see that...
        return store.get_validation_round(int(round_row["id"]))
    round_row = store.open_validation_round(
        wo_id=wo["id"], fingerprint=evidence_mod.fingerprint(packet),
        summary=str(wo.get("result_summary") or ""), evidence=declared,
        pr_url=wo.get("pr_url"), round=nxt,
        config_version=current_config_version(), forced_reason=forced_reason,
        uncounted=uncounted, uncounted_cause=uncounted_cause)
    store.set_status(wo["id"], "validating")
    # No attention flag: a unit under review is the system working
    store.clear_attention(wo["id"])
    store.add_event(wo["id"], "validation_submitted",
                    {"round": round_row["round"], "round_id": round_row["id"],
                     "fingerprint": round_row["fingerprint"],
                     "files": len(packet.files)})
    return round_row


def _bounce(store: ProjectStore, wo: dict[str, Any], previous: dict[str, Any],
            cited: tuple[str, ...], would_be: int) -> None:
    """Send this submission back to the worker without convening the panel"""
    wo_id = str(wo["id"])
    n = int(previous["round"])
    store.add_event(wo_id, "validation_bounced",
                    {"after_round": n, "round_id": int(previous["id"]),
                     "would_be_round": would_be, "cited": list(cited)})
    bus.post(store, subject=bus.Subject(wo_id=wo_id),
             from_role="reviewer", to_role="implementor",
             payload=bus.ReviewFeedback(
                 round=n, outcome="rejected",
                 reason=BOUNCE_FEEDBACK.format(n=n, wo_id=wo_id,
                                               paths=", ".join(cited))))


#: Check 4 passed unevaluated, from THIS side of the seam
_CHECK_4_NOT_RUN = ("check 4, that `{primitive}` can actually run against the triggering "
                    "order, was not run: {why}.")

#: The whole pre-check failed, so NONE of §7.2's four checks ran
_PRECHECK_NOT_RUN = ("checks 1-4, the whole mechanical pre-check of this fix order's "
                     "detector and remedy, were not run: the pre-check itself could not "
                     "be completed ({why}).")


def fix_order_precheck(store: ProjectStore, project_path: Path, wo: Mapping[str, Any],
                       *, central: Any = None,
                       unproven: list[str] | None = None) -> str | None:
    """Why this FIX ORDER may not be judged yet, in `rules.precheck`'s words, or None (docs/superpowers/specs/2026-09-27-self-evolution.md)"""
    # `rules` is imported at module level; `remedies` is not, and this is the only reader of it here
    from . import remedies as remedies_mod

    row = dict(wo or {})
    if not str(row.get("gap_class") or "").strip():
        return None
    own = central is None
    try:
        # INSIDE the try: a `CentralStore()` that cannot be opened is the same unreadable input as a row that...
        central = CentralStore() if own else central
        project = central.project_name_for_path(project_path)
        detectors = central.detectors_for_gap(str(row["gap_class"]).strip(), project)
        # The canonical shape `rules._rows_for` wants: detector id -> rows
        remedy_rules = {str(d.get("id") or ""):
                        central.remedy_rules_for(str(d.get("id") or ""))
                        for d in detectors}
        trigger, unreadable, missing = _triggering_order(store, row)
        trigger_facts = None
        if trigger is not None:
            try:
                trigger_facts = rules.facts(store, trigger, now=time.time())
            except Exception as exc:  # noqa: BLE001 — see the docstring's pinned ruling
                log.warning("could not build the facts of %s, the order %s is proved "
                            "against: %s", trigger.get("id"), row.get("id"), exc)
                unreadable = (f"its recorded state could not be read ({exc})")

        def precondition(primitive: str, params: Mapping[str, Any]) -> str:
            """Check 4's seam: `remedies.REMEDIES[primitive].can_apply` against the SAME subject the condition was..."""
            remedy = remedies_mod.REMEDIES.get(primitive)
            if remedy is None or trigger is None:
                # No registry entry to ask, or no subject to ask it about
                rules.note_unproven(unproven, _CHECK_4_NOT_RUN.format(
                    primitive=primitive,
                    why=("there is no registry entry for that primitive, or no "
                         "triggering order to ask it about")))
                return ""
            try:
                return str(remedy.can_apply(store, dict(trigger),
                                            dict(params or {})) or "")
            except Exception as exc:  # noqa: BLE001 — a predicate that RAISED produced
                # no answer either, so the same thing happens: check 4 is passed unevaluated and the submission goes...
                log.warning("the `can_apply` of %s raised on %s: %s",
                            primitive, trigger.get("id"), exc)
                rules.note_unproven(unproven, _CHECK_4_NOT_RUN.format(
                    primitive=primitive,
                    why=f"its `can_apply` raised against "
                        f"{trigger.get('id')} ({exc})"))
                return ""

        return rules.precheck(detectors, remedy_rules, row, trigger_facts,
                              precondition=precondition,
                              trigger_unreadable=unreadable,
                              trigger_missing=missing,
                              unproven=unproven)
    except Exception as exc:  # noqa: BLE001 — the pinned ruling again: UNREADABLE, not
        # refused
        log.exception("the self-evolution pre-check on %s could not be run",
                      row.get("id"))
        rules.note_unproven(unproven, _PRECHECK_NOT_RUN.format(why=exc))
        return None
    finally:
        if own and central is not None:
            central.close()


def _triggering_order(
        store: ProjectStore,
        wo: Mapping[str, Any]) -> tuple[dict[str, Any] | None, str, str]:
    """The order this fix order's rule is proved against"""
    named = str(wo.get("trigger_wo_id") or "").strip()
    if named:
        try:
            return store.get_work_order(named), "", ""
        except KeyError:
            return None, "", named
        except Exception as exc:  # noqa: BLE001 — the store could not answer at all:
            # UNREADABLE, the pinned kn-b7deec8f direction, and NOT the refusal above
            log.warning("the triggering order %s named by %s could not be read: %s",
                        named, wo.get("id"), exc)
            return None, (f"the triggering order {named} could not be read from this "
                          f"project's records ({exc})"), ""
    metadata = db.from_json(wo.get("metadata"), {}) or {}
    io_id = str(metadata.get(ORIGIN_IO_KEY) or "")
    if not io_id:
        return None, "", ""
    try:
        _name, _path, io = find_feature_order(io_id, None)
        refs = evidence_refs(io)
    except Exception as exc:  # noqa: BLE001 — a back-link that does not resolve is a
        # fact about the OS's records, not an error: the refusal below says so
        log.warning("the improvement order %s behind %s could not be read: %s",
                    io_id, wo.get("id"), exc)
        return None, "", ""
    for ref in refs:
        try:
            return store.get_work_order(str(ref)), "", ""
        except KeyError:
            continue
    return None, "", ""


def _precheck_refused(store: ProjectStore, wo: Mapping[str, Any], reason: str,
                      would_be: int) -> None:
    """Send a fix order back without convening the panel and without opening a round"""
    wo_id = str(wo["id"])
    store.add_event(wo_id, "validation_refused",
                    {"would_be_round": would_be,
                     "gap_class": str(wo.get("gap_class") or ""),
                     "detector_id": str(wo.get("detector_id") or ""),
                     "remedy_rule_id": str(wo.get("remedy_rule_id") or ""),
                     "trigger_wo_id": str(wo.get("trigger_wo_id") or ""),
                     "reason": reason})
    bus.post(store, subject=bus.Subject(wo_id=wo_id),
             from_role="reviewer", to_role="implementor",
             payload=bus.ReviewFeedback(
                 round=would_be, outcome="rejected",
                 reason=PRECHECK_FEEDBACK.format(reason=reason, wo_id=wo_id)))


def force_validation_refusal(store: ProjectStore, wo: dict[str, Any], *,
                             project: str, cfg: Any) -> str | None:
    """Why a round CANNOT be forced on this work order, in the sentence that refuses it"""
    if cfg is None or not cfg.enabled:
        return (f"the validation panel is off for {project}, so there is nothing to "
                f"force a round with — turn it on (`jarvis config set {project} "
                f"validation.enabled true`) before forcing a round")
    wo_id = str(wo["id"])
    status = str(wo["status"] or "")
    if status not in FORCEABLE_STATUSES:
        return (f"{wo_id} is {status}, and a round can only be forced on a work order "
                f"that has DELIVERED and whose worker is not typing — "
                f"{' or '.join(FORCEABLE_STATUSES)}. A settled order would be reopened "
                f"by a verdict that could change nothing; a live one would have its "
                f"session claimed by the round machine while its worker is still "
                f"writing to it")
    if not str(wo.get("pr_url") or ""):
        return (f"{wo_id} carries no pull request, so a fresh round would read its "
                f"worktree and record no commit — which is the state this command "
                f"exists to get out of. Give it its pull request first "
                f"(`jarvis wo finish {wo_id} --pr <url>`)")
    # ONE READ, predicate and wording derived from it (kn-08f2ff9b): the panel opens rounds on its own...
    latest = store.latest_validation_round(wo_id=wo_id)
    if ProjectStore.round_machine_owns(latest):
        assert latest is not None  # `round_machine_owns` is False for None
        return (f"round {latest['round']} on {wo_id} is {latest['outcome']} — the panel "
                f"has not finished with it. Wait for the verdict; forcing a round now "
                f"would judge the same pull request twice")
    return None


def force_validation(wo_id: str, *, reason: str,
                     project_name: str | None = None) -> dict[str, Any]:
    """`jarvis validation force` — a PERSON opening a fresh round, with no worker"""
    reason = reason.strip()
    if not reason:
        raise OpsError("`--reason` cannot be blank: it is what makes this round legible "
                       "afterwards as one a person forced, and why")
    name, path, _wo = find_work_order(wo_id, project_name)
    cfg = validation_config(name)
    store = ProjectStore(path)
    try:
        wo = store.get_work_order(wo_id)
        status = str(wo["status"] or "")
        refusal = force_validation_refusal(store, wo, project=name, cfg=cfg)
        if refusal:
            raise OpsError(refusal)
        round_row = submit_for_validation(store, path, wo,
                                          declared=declared_evidence(store, wo_id),
                                          cfg=cfg, forced_reason=reason)
        # AFTER the round exists, so the event can name it
        store.add_event(wo_id, "validation_forced",
                        {"round": round_row["round"], "round_id": round_row["id"],
                         "reason": reason, "was": status})
        return {"project": name, "wo_id": wo_id, "round": round_row["round"],
                "round_id": round_row["id"], "reason": reason, "was": status,
                "status": store.get_work_order(wo_id)["status"]}
    finally:
        store.close()


#: The `forced_reason` an OS-forced round carries
REJUDGE_FORCED_REASON = ("the OS re-judged this itself: round {n} passed on {judged}, "
                         "and the head of the pull request is now {head} — nothing "
                         "merges a commit no round has read")

#: The `forced_reason` for the OTHER unusable binding: the round passed and recorded NO commit (docs/superpowers/specs/2026-10-16-a-verdict-bound-to-no-commit-re-judges-itself.md §3)
SHA_UNRECORDED_FORCED_REASON = (
    "the OS re-judged this itself: round {n} passed without recording which commit it "
    "judged — it read a worktree rather than the pull request — so this round judges "
    "the pull request at {head}")

#: `validation_forced.by` for a round no person asked for
REJUDGE_BY_OS = "os"

#: The `forced_reason` a REBIND carries: the round the OS opens on a merge it demanded itself (docs/superpowers/specs/2026-09-27-a-conflict-resolution-the-os-asked-for-costs-no-round.md §4.3)
REBIND_FORCED_REASON = (
    "the OS re-judged this itself, outside the round budget: round {n} passed on "
    "{judged}, and the head is now {head} — the merge the OS asked for resolved a "
    "conflict, so the content changed and a seat has to read it. This round does not "
    "count against validation.max_rounds (rebind {used} of {max})")

#: The `forced_reason` a USER-REWORK round carries: the work the user asked for when they rejected on...
USER_REWORK_FORCED_REASON = (
    "the OS re-judged this itself, outside the round budget: the USER rejected this on "
    "review, round {n} judged {judged}, and the head is now {head} — the rework they "
    "asked for has to be judged. Rounds bound the worker-versus-panel loop and this is "
    "not part of it, so this round does not count against validation.max_rounds")

#: `cause` on `REJUDGE_DECLINED_EVENT`, one value per condition
REBIND_EXHAUSTED = "rebind_exhausted"
REJUDGE_BUDGET_SPENT = "budget"


def rebind_possible(store: ProjectStore, wo: dict[str, Any], *, head: str,
                    project: str, cfg: Any) -> bool:
    """Could a REBIND be opened on this head right now? (docs/superpowers/specs/2026-09-27-a-conflict-resolution-the-os-asked-for-costs-no-round.md §4.2)"""
    from . import worker_session

    wo_id = str(wo["id"])
    if not head or head in rejudged_heads(store, wo_id):
        return False
    if head in rejudged_heads(store, wo_id, declined=True):
        return False
    if worker_session.busy(store, wo_id) or store.queued_messages(wo_id):
        return False
    if force_validation_refusal(store, wo, project=project, cfg=cfg) is not None:
        return False
    return store.uncounted_validation_rounds(
        wo_id=wo_id, cause=REBIND_CAUSE) < REBIND_MAX


def rejudged_heads(store: ProjectStore, wo_id: str, *, declined: bool = False
                   ) -> set[str]:
    """The head commits the OS has already re-judged"""
    seen = set()
    if declined:
        for event in store.events_of_kind(wo_id, invariants.REJUDGE_DECLINED_EVENT):
            seen.add(str(db.from_json(event["payload"], {}).get("head_sha") or ""))
    else:
        for event in store.events_of_kind(wo_id, "validation_forced"):
            payload = db.from_json(event["payload"], {})
            if str(payload.get("by") or "") == REJUDGE_BY_OS:
                seen.add(str(payload.get("head_sha") or ""))
    seen.discard("")
    return seen


def stale_base_bases(store: ProjectStore, wo_id: str, head_sha: str) -> set[str]:
    """The base tips a merge of THIS commit has already been refused against"""
    seen = set()
    for event in store.events_of_kind(wo_id, MERGE_BASE_STALE_EVENT):
        payload = db.from_json(event["payload"], {}) or {}
        if str(payload.get("head_sha") or "") == head_sha:
            seen.add(str(payload.get("base_oid") or ""))
    return seen


def rejudge_moved_head(store: ProjectStore, project_path: Path, wo: dict[str, Any], *,
                       project: str, cfg: Any,
                       decision: Any,
                       rebind: bool = False) -> dict[str, Any] | None:
    """The OS re-opening a round on a pull request whose head moved out from under its verdict (docs/superpowers/specs/2026-09-27-a-conflict-resolution-the-os-asked-for-costs-no-round.md §4.3)"""
    from . import worker_session

    wo_id = str(wo["id"])
    head = str(getattr(decision, "head_sha", "") or "")
    # No head to bind a verdict to — `gh` could not read one this tick
    if not head or head in rejudged_heads(store, wo_id):
        return None
    if worker_session.busy(store, wo_id) or store.queued_messages(wo_id):
        return None
    if force_validation_refusal(store, wo, project=project, cfg=cfg) is not None:
        return None
    judged = str(getattr(decision, "judged_sha", "") or "")
    round_n = int(getattr(decision, "round_n", 0) or 0)
    # THE BUDGET POSITION, never a row number: after the rebind the two can differ
    budget_nxt = store.counted_validation_rounds(wo_id=wo_id) + 1
    # THE REBIND BOUND COUNTS REBINDS AND NOTHING ELSE: a user-rework round that spent one of these would...
    used = store.uncounted_validation_rounds(wo_id=wo_id, cause=REBIND_CAUSE)
    # A REWORK THE USER ASKED FOR IS JUDGED WHATEVER THE BUDGET SAYS
    rework = user_rework_pending(store, wo_id)
    if rework:
        pass                            # no bound at all: one free round per rejection
    elif rebind:
        if used >= REBIND_MAX:
            if head in rejudged_heads(store, wo_id, declined=True):
                return None             # said once per commit, not once per tick
            store.add_event(wo_id, invariants.REJUDGE_DECLINED_EVENT,
                            {"head_sha": head, "judged_sha": judged, "round": round_n,
                             "cause": REBIND_EXHAUSTED, "rebinds": used,
                             "rebind_max": REBIND_MAX})
            return {"wo_id": wo_id, "declined": True, "cause": REBIND_EXHAUSTED,
                    "head_sha": head, "judged_sha": judged, "rebinds": used,
                    "rebind_max": REBIND_MAX}
    elif budget_nxt >= int(cfg.max_rounds):
        if head in rejudged_heads(store, wo_id, declined=True):
            return None                 # said once per commit, not once per tick
        store.add_event(wo_id, invariants.REJUDGE_DECLINED_EVENT,
                        {"head_sha": head, "judged_sha": judged, "round": round_n,
                         "cause": REJUDGE_BUDGET_SPENT,
                         "next_round": budget_nxt, "max_rounds": int(cfg.max_rounds)})
        return {"wo_id": wo_id, "declined": True, "cause": REJUDGE_BUDGET_SPENT,
                "head_sha": head, "judged_sha": judged, "next_round": budget_nxt,
                "max_rounds": int(cfg.max_rounds)}
    was = str(wo["status"] or "")
    if rework:
        reason = USER_REWORK_FORCED_REASON.format(n=round_n, judged=judged[:10],
                                                  head=head[:10])
    elif rebind:
        reason = REBIND_FORCED_REASON.format(n=round_n, judged=judged[:10],
                                             head=head[:10], used=used + 1,
                                             max=REBIND_MAX)
    elif not judged:
        # Nothing to name, so nothing is claimed about it (spec 2026-10-16 §3)
        reason = SHA_UNRECORDED_FORCED_REASON.format(n=round_n, head=head[:10])
    else:
        reason = REJUDGE_FORCED_REASON.format(n=round_n, judged=judged[:10],
                                              head=head[:10])
    cause = USER_REWORK_CAUSE if rework else (REBIND_CAUSE if rebind else "")
    round_row = submit_for_validation(store, project_path, wo,
                                      declared=declared_evidence(store, wo_id),
                                      cfg=cfg, forced_reason=reason,
                                      uncounted=bool(cause), uncounted_cause=cause)
    store.add_event(wo_id, "validation_forced",
                    {"round": round_row["round"], "round_id": round_row["id"],
                     "reason": reason, "was": was, "by": REJUDGE_BY_OS,
                     # THE CAUSE, and `rebind` beside it for the rows written before it existed
                     "cause": cause, "rebind": cause == REBIND_CAUSE,
                     "head_sha": head, "judged_sha": judged})
    return {"wo_id": wo_id, "declined": False, "round": round_row["round"],
            "round_id": round_row["id"], "head_sha": head, "judged_sha": judged,
            "reason": reason, "was": was}


def force_validation_state(store: ProjectStore, wo: dict[str, Any], *, project: str,
                           held: dict[str, Any] | None) -> dict[str, Any] | None:
    """What the work-order page's "re-judge this pull request" control shows, or None"""
    from . import automerge

    cfg = validation_config(project)
    if cfg is None or not cfg.enabled:
        return None
    refusal = force_validation_refusal(store, wo, project=project, cfg=cfg)
    state: dict[str, Any] = dict(held or {})
    # A STALE HOLD IS TREATED AS NO HOLD (2026-09-27 spec §3): diagnosing from one would say the head...
    if not automerge.hold_is_live(state):
        state = {}
    code = str(state.get("code") or "")
    judged = str(state.get("judged_sha") or "")
    head = str(state.get("head_sha") or "")
    diagnosis = ""
    if code == automerge.HELD_SHA_MOVED:
        diagnosis = (f"The panel judged {judged[:10]}; the head of the pull request is "
                     f"now {head[:10] or 'unknown'}. They are not the same commit, and "
                     f"nothing merges a commit no round judged — a fresh round is what "
                     f"binds a verdict to the one that is there now.")
    elif code == automerge.HELD_SHA_UNRECORDED:
        diagnosis = (f"Round {state.get('round')} passed, but it never recorded WHICH "
                     f"commit it judged — it read a worktree rather than the pull "
                     f"request. Nothing can bind a merge to a diff until a round records "
                     f"one.")
    return {"can_force": refusal is None, "refusal": refusal, "diagnosis": diagnosis,
            "judged_sha": judged, "head_sha": head}


def forced_round_lines(result: dict[str, Any]) -> list[str]:
    """What forcing a round REPORTED, in two lines: the round it opened, and where the work order went"""
    return [f"{result['wo_id']} [{result['project']}]: round {result['round']} opened by "
            f"hand — {result['reason']}",
            f"was {result['was']}, now {result['status']}; the panel judges it on the "
            f"daemon's next tick"]


def forced_round_notice(store: ProjectStore, wo: dict[str, Any], *, project: str,
                        round_n: int) -> list[str]:
    """`forced_round_lines` for a page that has just redirected after forcing a round"""
    from . import db

    for event in store.events_of_kind(str(wo["id"]), "validation_forced"):
        payload = db.from_json(event["payload"], {})
        if int(payload.get("round") or 0) != round_n:
            continue
        return forced_round_lines({
            "wo_id": wo["id"], "project": project, "round": round_n,
            "reason": payload.get("reason") or "", "was": payload.get("was") or "",
            "status": wo["status"]})
    return []


def prior_round_history(store: ProjectStore, *, wo_id: str | None = None,
                        fo_id: str | None = None,
                        before: int) -> list[dict[str, Any]]:
    """What earlier rounds of this unit's review asked for, for `EvidencePacket.history` (docs/superpowers/specs/2026-09-15-the-panel-blocks-on-blockers.md §5.2.1)"""
    from . import validation
    from .project_store import COUNTED_VALIDATION_OUTCOMES

    out: list[dict[str, Any]] = []
    for r in store.validation_rounds(wo_id=wo_id, fo_id=fo_id):
        if int(r["round"]) >= before:
            continue
        if str(r["outcome"] or "") not in COUNTED_VALIDATION_OUTCOMES:
            continue
        raised: list[dict[str, str]] = []
        for op in store.validation_opinions(int(r["id"])):
            raised += validation.blockers(validation.findings(op))
        out.append({"round": int(r["round"]), "outcome": str(r["outcome"] or ""),
                    "reason": str(r["reason"] or ""),
                    "head_sha": str(r["head_sha"] or ""), "blockers": raised})
    return out


#: The daemon's own back-fill row (`Daemon.MERGE_COMMIT_EVENT`)
MERGE_COMMIT_EVENT = "pr_merge_commit_recorded"


class UnintegratedChild(Exception):
    """A completed child's merge commit is not in the head a round would be judged over"""

    def __init__(self, child_id: str, commit: str, head: str) -> None:
        super().__init__(invariants.FEATURE_CHILD_NOT_INTEGRATED.format(
            id=child_id, commit=commit, head=head))
        self.child_id = child_id
        self.commit = commit
        self.head = head


def recorded_merge_commit(store: ProjectStore, child: dict[str, Any]) -> str:
    """The commit this child put on the default branch, from the RECORD alone"""
    for kind in ("pr_merged", MERGE_COMMIT_EVENT):
        rows = store.events_of_kind(child["id"], kind)
        if rows:
            sha = db.from_json(rows[-1]["payload"], {}).get("merge_commit") or ""
            if sha:
                return str(sha)
    return ""


def unintegrated_children(project_path: Path, store: ProjectStore, fo_id: str, head: str,
                          merge_commit: Callable[[dict[str, Any]], str],
                          ) -> list[tuple[dict[str, Any], str]]:
    """Which of this feature's completed children are missing from `head` (docs/superpowers/specs/2026-10-07-a-feature-round-must-judge-a-head-that-contains-its-children.md)"""
    from . import branchproof

    missing: list[tuple[dict[str, Any], str]] = []
    for child in store.feature_children(fo_id, kinds=WORKER_CHILDREN):
        # The children `settle_features` counted
        if child["superseded"] or child["status"] != "completed":
            continue
        sha = merge_commit(child)
        if sha and not branchproof.is_ancestor(project_path, sha, head):
            missing.append((child, sha))
    return missing


def collect_feature_evidence(store: ProjectStore, project_path: Path,
                             fo: dict[str, Any], *, declared: str, summary: str,
                             cfg: Any,
                             history: Iterable[dict[str, Any]] = ()) -> Any:
    """The feature's packet, with each child's own account attached"""
    from . import evidence as evidence_mod

    children = []
    for child in store.feature_children(fo["id"], kinds=WORKER_CHILDREN):
        last = store.latest_validation_round(wo_id=child["id"])
        # `merge_commit` is what scopes the feature's diff to its own children (issue 985)
        children.append({**child, "declared": str((last or {}).get("evidence") or ""),
                         "merge_commit": recorded_merge_commit(store, child)})
    return evidence_mod.collect_feature(project_path, fo, children, declared=declared,
                                        summary=summary, diff_chars=cfg.diff_chars,
                                        side_effects=feature_side_effects(store,
                                                                          fo["id"]),
                                        history=history)


def submit_feature_for_validation(store: ProjectStore, project_path: Path,
                                  fo: dict[str, Any], *, declared: str, summary: str,
                                  cfg: Any) -> dict[str, Any]:
    """Open a validation round over the feature as a whole, and park it in `validating` (docs/superpowers/specs/2026-10-07-a-feature-round-must-judge-a-head-that-contains-its-children.md)"""
    from . import branchproof
    from . import evidence as evidence_mod

    # §1: the same ladder the collector resolves its head with
    ref = evidence_mod.base_ref(project_path)
    if ref and not branchproof.fetch(project_path, ref):
        log.warning("could not fetch %s in %s — %s is being judged on the local ref as "
                    "it stands", ref, project_path, fo["id"])
        # §2 + Neo 1394: a stale local ref is exactly how a round gets opened over a head that predates a...
        head = evidence_mod.default_branch_head(project_path)
        missing = unintegrated_children(
            project_path, store, fo["id"], head,
            lambda child: recorded_merge_commit(store, child)) if head else []
        if missing:
            child, sha = missing[0]
            raise UnintegratedChild(child["id"], sha, head)
    packet = collect_feature_evidence(store, project_path, fo, declared=declared,
                                      summary=summary, cfg=cfg)
    fo_id = fo["id"]
    # One expression for "how a round is numbered"
    nxt = store.numbered_validation_rounds(fo_id=fo_id) + 1
    round_row = store.open_validation_round(
        fo_id=fo_id, fingerprint=evidence_mod.fingerprint(packet), summary=summary,
        evidence=declared, round=nxt, config_version=current_config_version())
    store.set_feature_status(fo_id, "validating")
    # No attention flag: a unit under review is the system working
    store.clear_feature_attention(fo_id)
    feature_event(store, fo_id, "validation_submitted",
                  {"round": round_row["round"], "round_id": round_row["id"],
                   "fingerprint": round_row["fingerprint"],
                   "files": len(packet.files), "feature_order": fo_id})
    return round_row


def feature_event(store: ProjectStore, fo_id: str, kind: str,
                  payload: dict[str, Any]) -> bool:
    """Write one feature-order event onto the timeline that carries it"""
    manager = store.manager_work_order(fo_id)
    if not manager:
        return False
    store.add_event(manager["id"], kind, payload)
    return True


def feature_events_of_kind(store: ProjectStore, fo_id: str,
                           kind: str) -> list[dict[str, Any]]:
    """Read back what `feature_event` wrote"""
    manager = store.manager_work_order(fo_id)
    return store.events_of_kind(manager["id"], kind) if manager else []


def submit_feature(fo_id: str, summary: str, evidence: str = "",
                   project_name: str | None = None) -> dict[str, Any]:
    """`jarvis fo submit` — the project manager saying the feature is ready again"""
    name, path, fo = find_feature_order(fo_id, project_name)
    if fo["status"] != "executing":
        raise OpsError(
            f"{fo_id} is {fo['status']}, not executing — there is nothing to submit. A "
            f"feature order can only be submitted for review while its work orders are "
            f"the thing in flight.")
    cfg = validation_config(name)
    if cfg is None or not cfg.enabled or not cfg.feature_units:
        return {"project": name, "fo_id": fo_id, "status": fo["status"], "opened": False,
                "note": "validation of feature orders is switched off, so no review "
                        "round was opened; this feature settles when its work orders do"}
    store = ProjectStore(path)
    try:
        round_row = submit_feature_for_validation(
            store, path, store.get_feature_order(fo_id), declared=evidence,
            summary=summary, cfg=cfg)
    finally:
        store.close()
    return {"project": name, "fo_id": fo_id, "status": "validating", "opened": True,
            "round": round_row["round"], "fingerprint": round_row["fingerprint"]}


def declared_evidence(store: ProjectStore, wo_id: str) -> str:
    """What the worker last said it did to test this, recovered from its own `finish`"""
    for event in reversed(store.events_of_kind(wo_id, "finished")):
        text = db.from_json(event["payload"], {}).get("evidence")
        if text:
            return str(text)
    return ""


def gate_still_open(wo_id: str, request: dict[str, Any]) -> str:
    """Why this work order cannot settle yet, and the one way on from where it is"""
    from .gates import AWAITING_CASE, exits_advice

    if request["status"] == AWAITING_CASE:
        # Both exits, from the renderer every other block uses
        way_on = ("Nobody is reviewing it: it carries neither a case nor a contest.\n\n"
                  + exits_advice(wo_id, request["command"], request["kind"],
                                 request["id"]))
    else:
        way_on = ("It is under review. End your turn — the verdict arrives as your "
                  "next user turn, and you finish from there.")
    return (f"{wo_id} has gate request {request['id']} ({request['kind']}) still open, "
            f"so it cannot be finished: settling it now would close the work order over "
            f"a privileged action nobody ruled on. {way_on}")


def unlanded_refusal(wo: dict[str, Any], summary: str, work: landing.Authored) -> str:
    """Why this work order may not finish, and only the ways on its OWN kind may take (#232)"""
    from . import landing as landing_mod

    wo_id = str(wo["id"])
    contract = kind_contract(wo)
    if not contract.lands_code:
        # NO LANDING ROUTE TO OFFER: commit, push and `--pr` are outside this kind's write surface
        submit = contract.submit.format(fo_id=wo.get("parent_id") or "<order-id>")
        files = ", ".join(f"`{f}`" for f in work.dirty[:5]) or work.describe()
        return (
            f"{wo_id} is the {wo.get('kind')} of its order: it does not land code, so "
            f"nothing it leaves in its worktree can be delivered. Its artifact is "
            f"`{contract.artifact}` and its submission is `{submit}` — but the worktree "
            f"also holds {files}, which no submission consumes and no command of yours "
            f"may land. End your turn naming those files: what happens to them is the "
            f"user's call."
        )
    named = landing_mod.pr_urls_in(summary)
    if named:
        lead = (f"Your summary names {named[0]}, but you did not pass it as `--pr`. "
                f"Finishing like this records NO pull request, so nothing would ever "
                f"watch it merge and {work.describe()} would stay on the branch.")
        fix = f"Finish again with `--pr {named[0]}`."
    else:
        lead = (f"{wo_id} has produced {work.describe()}, none of it on `{work.base}`, "
                f"and there is no pull request to land it.")
        fix = ("Commit and push anything outstanding, open a pull request, and finish "
               "again with `--pr <url>`.")
    return (
        f"{lead}\n\n{fix}\n\n"
        f"If this work is deliberately NOT being landed, that is a legitimate outcome "
        f"and the OS only asks that you say so: re-run with "
        f"`--abandon \"<why it is not being landed>\"`. What it will not accept is "
        f"silence — six work orders reached `completed` with their code on nothing but "
        f"a branch, and nothing noticed for seven weeks (GitHub issue #232)."
    )


def finish(wo_id: str, summary: str, pr_url: str | None = None,
           evidence: str = "", abandon: str = "") -> dict[str, Any]:
    """The worker reporting its own result (docs/superpowers/specs/2026-09-13-two-gates-not-a-chain.md §1)"""
    name, path, _wo = find_work_order(wo_id)
    cfg = validation_config(name)
    store = ProjectStore(path)
    try:
        open_requests = store.open_approvals(wo_id)
        if open_requests:
            # WHICH request the message describes is a choice
            from .gates import AWAITING_CASE

            raise OpsError(gate_still_open(wo_id, next(
                (a for a in open_requests if a["status"] == AWAITING_CASE),
                open_requests[0])))
        # Read ONCE, before anything is written: a refused finish must leave the record exactly as it found...
        stranding = unlanded_work(store, _wo, pr_url or "")
        # A pushed branch is undertaken to land, not refused; `land_finished` parks it (docs/superpowers/specs/2026-10-12-a-pushed-branch-with-no-pull-request-is-a-state.md §2)
        if (stranding.produced and not abandon
                and not (stranding.pushed and kind_contract(_wo).lands_code)):
            raise OpsError(unlanded_refusal(_wo, summary, stranding))
        # `stranding` is the REFUSAL's reading and is deliberately empty for an order carrying a pull...
        work = stranding if stranding.base else authorship(store, _wo)
        fields: dict[str, Any] = {"result_summary": summary}
        if pr_url:
            fields["pr_url"] = pr_url
        store.update_work_order(wo_id, **fields)
        fresh = store.get_work_order(wo_id)
        # BEFORE anything settles, because settling reads it: `refusal_answered` dates the delivery against...
        store.add_event(wo_id, "finished",
                        {"summary": summary,
                         **({"pr_url": pr_url} if pr_url else {}),
                         **({"evidence": evidence} if evidence else {}),
                         **({"authored": work.record()} if work.base else {})})
        if abandon:
            # AFTER `finished` and never before: `_abandoned` reads the newer of the two
            store.add_event(wo_id, "abandoned",
                            {"reason": abandon, **work.record()})
            if _wo["status"] in TERMINAL_STATUSES:
                # RECORDING A DECISION ABOUT SETTLED WORK IS NOT A DELIVERY
                store.close_violation_report(invariants.INV_WORK_LANDED, wo_id)
                return {"project": name, "wo_id": wo_id, "status": _wo["status"]}
        # AFTER the `finished` event, so `refusal_answered` still dates correctly
        deferred = defer_red_release(store, name, fresh)
        if deferred is not None:
            return {"project": name, "wo_id": wo_id, "status": deferred,
                    **({"pr_url": pr_url} if pr_url else {})}
        # The red base is one reason a release cuts nothing; `completed` is wrong for every other
        unshipped = park_unshipped_release(store, fresh)
        if unshipped is not None:
            return {"project": name, "wo_id": wo_id, "status": unshipped,
                    **({"pr_url": pr_url} if pr_url else {})}
        opened = bounced = None
        # The overlay: `pr_url` is not written until `land_when_cleared` below (2026-10-01 spec §2
        submits = validation_applies(cfg, {**fresh, "pr_url": pr_url}, store)
        if submits:
            opened = submit_for_validation(store, path, fresh, declared=evidence,
                                           cfg=cfg)
            # None means BOUNCED, and only here: with validation off no submission was attempted at all
            bounced = opened is None
        if str((opened or {}).get("outcome") or "") == "escalated":
            # The bounce ceiling, and the ONE case where a submission has already settled itself: the user has...
            status = str(store.get_work_order(wo_id)["status"] or "")
        else:
            # ...and the status is the JOIN's to decide, not this branch's
            status = land_when_cleared(store, fresh, pr_url, panel_open=bool(bounced),
                                       panel_cleared=not submits)
    finally:
        store.close()
    return {"project": name, "wo_id": wo_id, "status": status,
            **({"pr_url": pr_url} if pr_url else {})}


def record_pull_request(wo_id: str, url: str, project_name: str | None = None,
                        source: str = "user") -> dict[str, Any]:
    """Record the pull request the user opened for a pushed-branch park; never a `finished` event (docs/superpowers/specs/2026-10-12-a-pushed-branch-with-no-pull-request-is-a-state.md §8)"""
    from . import github

    name, path, wo = find_work_order(wo_id, project_name)
    store = ProjectStore(path)
    try:
        # A declaration is never overwritten, as in `gates._record_pull_request`
        if wo.get("pr_url"):
            raise OpsError(
                f"{wo_id} already records {wo['pr_url']}. A pull request is declared "
                f"once; if that one is wrong, close the order with `jarvis wo done`.")
        if store.pr_pending_open(wo_id) is None:
            owed = true_blockers(store, wo)
            waiting = owed[0] if owed else f"{wo['status']}, with nothing owed"
            raise OpsError(
                f"{wo_id} has no pushed branch waiting on a pull request. "
                f"What it is waiting on: {waiting}.")
        try:
            url = github.checked_pr_url(url, store.project_path)
        except github.UntrustedPullRequest as e:
            raise OpsError(str(e)) from e
        store.update_work_order(wo_id, pr_url=url)
        store.add_event(wo_id, "pr_url_recorded", {"pr_url": url, "source": source})
        fresh = store.get_work_order(wo_id)
        status = fresh["status"]
        # Judge the row it would become before writing the status: the merge poll reads the status
        probe = {**fresh, "status": "waiting_pr_merge"}
        if status == "needs_review" and not true_blockers(store, probe):
            store.set_status(wo_id, "waiting_pr_merge")
            store.clear_attention(wo_id)
            status = "waiting_pr_merge"
        return {"project": name, "wo_id": wo_id, "pr_url": url, "source": source,
                "status": status}
    finally:
        store.close()


def mark_done(wo_id: str, project_name: str | None = None) -> dict[str, Any]:
    """The user closing a work order themselves: "this is finished, stop tracking it" (#232)"""
    name, path, wo = find_work_order(wo_id, project_name)
    store = ProjectStore(path)
    try:
        if store.pending_assumptions(wo_id):
            raise OpsError(
                f"{wo_id} is waiting on a decision (assumptions pending review) — "
                f"marking it done would accept them silently. Use `jarvis wo review "
                f"{wo_id}` to accept, or `--reject` to send it back."
            )
        stranding = unlanded_work(store, wo)
        unmerged = unmerged_pull_request(store, wo_id)
        if (stranding.produced or unmerged) and not store.work_abandoned(wo_id):
            store.add_event(wo_id, "work_unlanded",
                            {**stranding.record(),
                             **({"pr_url": unmerged} if unmerged else {}),
                             "was": wo["status"], "closed_by": "marked_done"})
        stopped = close_out(store, wo, "marked_done", why="work order marked done")
    finally:
        store.close()
    mark_backlog_done(wo)
    return {"project": name, "wo_id": wo_id, "title": wo["title"],
            "status": "completed", "was": wo["status"],
            "session_stopped": stopped["stopped"]}


def close_out(store: ProjectStore, wo: dict[str, Any], event: str, *, why: str,
              payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Settle a work order as `completed` and take its worker down with it, from OUTSIDE it"""
    if os.environ.get("JARVIS_WO_ID") == wo["id"]:
        # THE CALLER IS INSIDE THE SESSION THIS WOULD KILL, and every write after it (io-d2a5dd69)
        raise OpsError(
            f"refusing to close {wo['id']} from inside its own session: closing it stops "
            f"that session, and this process with it, before the close is recorded. A "
            f"work order settles itself through its own contract command "
            f"(`settle_in_session`), never through `close_out`.")
    stopped = stop_worker_session(wo, store)
    store.set_status(wo["id"], "completed")
    store.clear_attention(wo["id"])
    store.add_event(wo["id"], event, {**(payload or {}), "was": wo["status"],
                                      "session_stopped": stopped["stopped"]})
    if stopped["stopped"]:
        store.add_event(wo["id"], "session_stopped",
                        {**{k: v for k, v in stopped.items() if k != "stopped"},
                         "reason": why})
    return stopped


def settle_in_session(store: ProjectStore, wo: dict[str, Any], event: str, *,
                      payload: dict[str, Any] | None = None) -> None:
    """Settle a work order `completed` from INSIDE its own turn (io-d2a5dd69)"""
    store.set_status(wo["id"], "completed", trigger="submitted")
    store.clear_attention(wo["id"])
    store.add_event(wo["id"], event, {**(payload or {}), "was": wo["status"],
                                      "session_stopped": False})


def mark_backlog_done(wo: dict[str, Any]) -> None:
    """Close the backlog item a work order was promoted from, if it came from one"""
    if not wo.get("backlog_id"):
        return
    central = CentralStore()
    try:
        central.mark_backlog(wo["backlog_id"], "done")
    finally:
        central.close()


def void_round_for_settled_pr(store: ProjectStore, wo: dict[str, Any],
                              reason: str) -> int | None:
    """Close an open validation round whose question the pull request just answered"""
    rnd = store.latest_validation_round(wo_id=wo["id"])
    if not rnd or not store.round_machine_owns(rnd):
        return None
    store.settle_validation_round(
        rnd["id"], "void",
        {"round": rnd["round"], "round_id": rnd["id"], "reason": reason},
        wo_id=wo["id"], reason=reason)
    return int(rnd["round"])


def complete_merged(store: ProjectStore, wo: dict[str, Any],
                    merged_at: str | None = None,
                    head_oid: str = "",
                    merge_commit: str = "",
                    automerge: dict[str, Any] | None = None) -> dict[str, Any]:
    """The pull request landed: end the work order, exactly as the user closing it does (docs/superpowers/specs/2026-09-26-a-release-order-overtaken-mid-ci-wait-settles-itself.md §2)"""
    if automerge:
        store.add_event(wo["id"], "automerge_merged",
                        {**automerge, "pr_url": wo.get("pr_url")})
    store.update_work_order(wo["id"], pr_state="MERGED")
    stopped = close_out(store, wo, "pr_merged", why="pull request merged",
                        payload={"pr_url": wo.get("pr_url"), "merged_at": merged_at,
                                 **({"head_oid": head_oid} if head_oid else {}),
                                 **({"merge_commit": merge_commit}
                                    if merge_commit else {})})
    mark_backlog_done(wo)
    return {"wo_id": wo["id"], "status": "completed", "was": wo["status"],
            "pr_url": wo.get("pr_url"), "merged_at": merged_at,
            "session_stopped": stopped["stopped"]}


def record_pr_closed(store: ProjectStore, wo: dict[str, Any]) -> dict[str, Any]:
    """The pull request was closed without merging: the delivered work was refused"""
    store.update_work_order(wo["id"], pr_state="CLOSED")
    store.add_event(wo["id"], "pr_closed", {"pr_url": wo.get("pr_url"),
                                            "was": wo["status"]})
    store.set_status(wo["id"], "needs_review")
    # EVERY REPAIR EPISODE ENDS HERE, because there is nothing left to repair: the pull request is shut
    for repair in PR_REPAIRS:
        clear_pr_repair(store, wo, repair)
    # DERIVED, not asserted (#224)
    fresh = store.get_work_order(wo["id"])
    blockers = true_blockers(store, fresh)
    store.flag_attention(wo["id"], blockers[0] if blockers else PR_CLOSED_BLOCKER)
    return {"wo_id": wo["id"], "status": "needs_review", "was": wo["status"],
            "pr_url": wo.get("pr_url")}


#: Baked into every repair nudge, not a format field, so a new nudge cannot omit it
TARGETED_TESTS_LINE = ("run only the tests covering the files you touched (NOT the "
                       "full suite — that is CI's job, and it runs it on more "
                       "interpreters than you can)")


#: The nudge the user used to type by hand
PR_CONFLICT_NUDGE = """\
Your pull request {url} has merge conflicts with `{base}` and cannot be merged as it \
stands. GitHub reports it as CONFLICTING; nobody typed this message, Jarvis noticed \
while polling for the merge.

Resolve them: in your worktree, `git fetch origin`, merge `origin/{base}` into your \
branch, fix every conflict, """ + TARGETED_TESTS_LINE + """, and push. Do NOT rebase or \
force-push — a forced branch update is refused by the permission classifier. If the \
conflict is not resolvable from this branch (for instance the branch it was opened \
against has itself been merged), say so plainly in your final message rather than \
fighting it: that is a call for the user.

When the push is done, simply END YOUR TURN. This work order already finished and \
already has its summary — do NOT call `jarvis wo finish` again. Jarvis puts it back \
where it was by itself and re-checks the merge.

This is attempt {attempt} of {max_attempts}. After {max_attempts} the work order stops \
trying and asks the user."""


#: The same message for a red build (docs/superpowers/specs/2026-09-13-a-work-order-never-sits-on-a-red-pull-request.md §3.)
PR_CHECKS_NUDGE = """\
Your pull request {url} has failing checks and must not be merged as it stands. GitHub \
reports these as failed: {failing}. Nobody typed this message — Jarvis noticed while \
polling the pull request.

Fix them: in your worktree, reproduce each failure locally, fix the cause, \
""" + TARGETED_TESTS_LINE + """, and push. If a check fails for a reason that is not \
yours to fix (an \
infrastructure outage, a flake, a required check this branch cannot satisfy), say so \
plainly in your final message rather than fighting it: that is a call for the user. Do \
NOT rebase or force-push — a forced branch update is refused by the permission \
classifier.{behind}

When the push is done, simply END YOUR TURN. This work order already finished and \
already has its summary — do NOT call `jarvis wo finish` again. Jarvis puts it back \
where it was by itself and re-checks the build.

This is attempt {attempt} of {max_attempts}. After {max_attempts} the work order stops \
trying and asks the user."""

#: Appended to the nudge above when the branch is also BEHIND its base
PR_BEHIND_NOTE = """ The branch is also behind `{base}`, which this repository's ruleset \
requires it not to be before a merge; while you are in there, merge `origin/{base}` in \
(do not rebase) so the checks re-run against what would actually land."""


#: THE ONE REPAIR THAT INVERTS THE OTHER TWO: those work orders already declared themselves (docs/superpowers/specs/2026-10-07-an-undeclared-delivery-must-heal-itself.md §2.2.)
PR_UNDECLARED_NUDGE = """\
The user REFUSED an assumption on this work order, and you pushed commits since. The \
head of {url} is one nothing has declared, so the panel may not judge it and nobody is \
reading it. Nobody typed this message — Jarvis noticed while polling the pull request.

Declare it: run `jarvis wo finish <wo-id> --summary "..." --pr {url} --evidence "..."`. \
Say in the summary what the refusal asked for and what you changed, and in the evidence \
state what you ran; """ + TARGETED_TESTS_LINE + """.

Then END YOUR TURN. Do NOT push anything new to answer this message — the declaration \
is the whole ask, and the commits are already there.

This is attempt {attempt} of {max_attempts}. After {max_attempts} the work order stops \
trying and asks the user."""


@dataclass(frozen=True)
class PrRepair:
    """One thing a poll can ask a worker to fix on its own pull request (#224)"""

    #: Names the event kinds (`pr_<name>_nudged`/`_cleared`/`_unresolved`)
    name: str
    template: str
    blocker: str

    @property
    def source(self) -> str:
        return f"pr-{self.name}"

    def event(self, suffix: str) -> str:
        return f"pr_{self.name}_{suffix}"


PR_CONFLICT = PrRepair(invariants.PR_CONFLICT_REPAIR, PR_CONFLICT_NUDGE,
                       invariants.PR_CONFLICT_BLOCKER)
PR_CHECKS = PrRepair(invariants.PR_CHECKS_REPAIR, PR_CHECKS_NUDGE,
                     invariants.PR_CHECKS_BLOCKER)
PR_UNDECLARED = PrRepair(invariants.PR_UNDECLARED_REPAIR, PR_UNDECLARED_NUDGE,
                         invariants.PR_UNDECLARED_BLOCKER)

#: Newest episode first is not a thing here
PR_REPAIRS = (PR_CONFLICT, PR_CHECKS, PR_UNDECLARED)


def nudge_pr_repair(store: ProjectStore, wo: dict[str, Any], repair: PrRepair,
                    **fields: Any) -> dict[str, Any]:
    """GitHub says this pull request is broken: ask the worker to fix it, or give up"""
    attempts = store.pr_repair_attempts(wo["id"], repair.name)
    if attempts >= invariants.PR_REPAIR_MAX_ATTEMPTS:
        if not store.pr_repair_gave_up(wo["id"], repair.name):
            store.add_event(wo["id"], repair.event("unresolved"),
                            {"pr_url": wo.get("pr_url"), "attempts": attempts})
            store.flag_attention(wo["id"], repair.blocker)
        return {"wo_id": wo["id"], "nudged": False, "attempts": attempts,
                "gave_up": True}
    attempt = attempts + 1
    msg_id = store.queue_message(
        wo["id"],
        repair.template.format(
            url=wo.get("pr_url") or "your pull request",
            attempt=attempt, max_attempts=invariants.PR_REPAIR_MAX_ATTEMPTS, **fields),
        # Not "jarvis" and not "user": this message had no author
        source=repair.source,
    )
    store.add_event(wo["id"], repair.event("nudged"),
                    {"pr_url": wo.get("pr_url"), "attempt": attempt,
                     "of": invariants.PR_REPAIR_MAX_ATTEMPTS, "msg_id": msg_id,
                     "was": wo["status"],
                     **{k: v for k, v in fields.items() if k != "behind"}})
    return {"wo_id": wo["id"], "nudged": True, "attempts": attempt, "gave_up": False}


def defer_pr_repair(store: ProjectStore, wo: dict[str, Any], repair: PrRepair,
                    request: dict[str, Any]) -> bool:
    """Say once that a pending gate is why this repair is not being attempted (#469)"""
    if store.pr_repair_deferred(wo["id"], repair.name, request["id"]):
        return False
    store.add_event(wo["id"], repair.event("deferred"), {
        "pr_url": wo.get("pr_url"), "approval_id": request["id"],
        "kind": request["kind"],
        "attempts": store.pr_repair_attempts(wo["id"], repair.name)})
    return True


def rearm_pr_repair(store: ProjectStore, wo: dict[str, Any],
                    repair: PrRepair) -> int:
    """Give back a budget spent on turns the worker was never allowed to take (#469)"""
    if not store.pr_repair_gave_up(wo["id"], repair.name):
        return 0
    if store.events_of_kind(wo["id"], repair.event("rearmed")):
        return 0
    if store.pending_approvals(wo["id"]):
        return 0        # still shut — `Daemon.heal_pull_request`'s guard holds anyway
    nudges = store.pr_repair_nudges(wo["id"], repair.name)
    if not nudges or not all(store.gate_open_at(wo["id"], n["ts"]) for n in nudges):
        return 0
    store.add_event(wo["id"], repair.event("rearmed"), {
        "pr_url": wo.get("pr_url"), "attempts": len(nudges)})
    if wo["attention_reason"] == repair.blocker:
        store.clear_attention(wo["id"])
    return len(nudges)


def clear_pr_repair(store: ProjectStore, wo: dict[str, Any],
                    repair: PrRepair) -> bool:
    """The pull request is well again: close the episode (#705)"""
    if not store.pr_repair_attempts(wo["id"], repair.name):
        return False
    store.add_event(wo["id"], repair.event("cleared"), {"pr_url": wo.get("pr_url")})
    if wo["attention_reason"] == repair.blocker:
        store.clear_attention(wo["id"])
    resettle_after_repair(store, wo["id"])
    return True


def repaired_since_finish(store: ProjectStore, wo_id: str) -> bool:
    """Did a repair episode open AND close after this work order delivered? (#705)"""
    finished = store.events_of_kind(wo_id, "finished")
    if not finished:
        return False
    since = float(finished[-1]["ts"])
    return any(float(e["ts"]) > since
               for repair in PR_REPAIRS
               for e in store.events_of_kind(wo_id, repair.event("cleared")))


def resettle_after_repair(store: ProjectStore, wo_id: str) -> bool:
    """Put a repaired work order back in the merge queue if nothing else holds it (#705)"""
    wo = store.get_work_order(wo_id)
    if (wo["status"] != "needs_review" or not wo.get("result_summary")
            or not _awaiting_merge(wo)):
        return False
    if not repaired_since_finish(store, wo_id):
        return False
    # THE REPAIR MUST BE WHY IT IS HERE NOW, not merely the last thing that closed
    if store.turn_opened_by(store.latest_turn(wo_id)) not in invariants.PR_REPAIR_SOURCES:
        return False
    # The other repair's episode may still be open, and it owns the status while it is
    if pr_repair_origin(store, wo_id) or store.queued_messages(wo_id):
        return False
    # The `needs_review` triage in `true_blockers`
    if (store.pending_assumptions(wo_id)
            or invariants.validation_escalated(store, wo)
            or store.work_unlanded_open(wo_id)):
        return False
    # JUDGE THE ROW IT WOULD BECOME, BEFORE WRITING ANYTHING
    probe = dict(wo)
    probe["status"] = "waiting_pr_merge"
    if true_blockers(store, probe):
        return False
    # Spec §C of docs/superpowers/specs/2026-10-14-a-message-must-not-drop-an-owed-review.md
    store.set_status(wo_id, "waiting_pr_merge", trigger="pr_repair_resettled")
    store.add_event(wo_id, "pr_repair_resettled",
                    {"pr_url": wo.get("pr_url"), "was": wo["status"]})
    store.clear_attention(wo_id)
    return True


#: The event `carry_validated_head` writes
HEAD_CARRIED_EVENT = "validation_head_carried"

#: A carry the OS could NOT justify, and the reason the round machine is about to spend a round
CARRY_REFUSED_EVENT = "validation_carry_refused"

#: `cause` on `HEAD_CARRIED_EVENT`: the OS's own merge-ref rebuild
CARRY_BASE_HEAL = "base_heal"
CARRY_BASE_MERGE_CHAIN = "base_merge_chain"

#: `cause` on `PR_BASE_UPDATED_EVENT` / `PR_BASE_UPDATE_FAILED_EVENT`: which of the two reasons asked...
BASE_UPDATE_BEHIND = "behind"
BASE_UPDATE_BASE_RED = "base_red"

#: How many times the OS may catch one pull request up with a MOVING base
CATCH_UP_MAX = 3
#: ...WITHIN THIS WINDOW: a RATE, not a lifetime budget (#1215, Neo 2191)
CATCH_UP_WINDOW_SECONDS = 2 * 3600

#: A merge the OS refused because the base moved under the judged commit — spec 2026-09-28 §3.1
MERGE_BASE_STALE_EVENT = "automerge_base_stale"

#: WHAT A CATCH-UP DID, and the reason `CatchUp` exists: the old return type could not tell "already...
CATCH_UP_NOT_NEEDED = "not_needed"
CATCH_UP_DONE = "done"
CATCH_UP_DEFERRED = "deferred"
CATCH_UP_EXHAUSTED = "exhausted"
CATCH_UP_FAILED = "failed"

#: `CatchUp.cause` on a DEFERRED outcome: the three waits are not one wait (#1188)
CATCH_UP_DEFER_IN_FLIGHT = "in_flight"
CATCH_UP_DEFER_HEAD_MOVED = "head_moved"
#: The head moved off the judged commit AND no carry could be proven onto it
CATCH_UP_DEFER_CARRY_UNPROVEN = "carry_unproven"


@dataclass(frozen=True)
class CatchUp:
    """What `Daemon._catch_up_with_base` did, as data its caller can branch on"""

    pr: Any
    outcome: str
    reason: str = ""
    base_tip: str = ""
    #: `CATCH_UP_DEFER_*` on a DEFERRED outcome: WHICH wait, so a caller can answer it
    cause: str = ""


#: How many times the OS may RE-JUDGE a merge it asked for itself, outside the round budget (docs/superpowers/specs/2026-09-27-a-conflict-resolution-the-os-asked-for-costs-no-round.md §4.6)
REBIND_MAX = 2

#: The two reasons a round may be `uncounted`, written on the round as `uncounted_cause`
REBIND_CAUSE = "rebind"
USER_REWORK_CAUSE = "user_rework"
#: Spec docs/superpowers/specs/2026-10-13-a-red-head-is-sent-back-before-the-panel.md §3.3
RED_CI_CAUSE = "red_ci"

#: WHICH CONDITION ESCALATED A ROUND, written in the `validation_escalated` event payload (docs/superpowers/specs/2026-10-14-an-escalated-round-says-why-and-what-to-run-next.md §1)
ESCALATION_ROUNDS_SPENT = "rounds_spent"
ESCALATION_USER_REWORK_CEILING = "user_rework_ceiling"
ESCALATION_REBINDS_SPENT = "rebinds_spent"
ESCALATION_BOUNCE_EXHAUSTED = "bounce_exhausted"
ESCALATION_NOTHING_TO_JUDGE = "nothing_to_judge"
ESCALATION_REPEAT_SUBMISSION = "repeat_submission"
ESCALATION_NO_VERDICT = "no_verdict"
ESCALATION_VALIDATOR_UNREACHABLE = "validator_unreachable"
ESCALATION_PROMPT_REFUSED = "prompt_refused"
#: A KILL IS NOT AN OUTAGE: the seats were reached and were killed by a signal (docs/superpowers/specs/2026-10-14-a-killed-validation-panel-opens-an-investigation.md §4)
ESCALATION_PANEL_KILLED = "panel_killed"
#: READ-TIME FALLBACK, never written by any call site: every `validation_escalated` event written...
ESCALATION_CAUSE_UNRECORDED = "unrecorded"

#: THREE WRITTEN FAMILIES, and the family decides the VERB alone
ESCALATION_FAMILY: dict[str, str] = {
    ESCALATION_ROUNDS_SPENT: "handover",
    ESCALATION_USER_REWORK_CEILING: "handover",
    ESCALATION_REBINDS_SPENT: "handover",
    ESCALATION_BOUNCE_EXHAUSTED: "not_reviewed",
    ESCALATION_NOTHING_TO_JUDGE: "not_reviewed",
    ESCALATION_REPEAT_SUBMISSION: "not_reviewed",
    ESCALATION_NO_VERDICT: "unjudged",
    ESCALATION_VALIDATOR_UNREACHABLE: "unjudged",
    ESCALATION_PROMPT_REFUSED: "unjudged",
    ESCALATION_PANEL_KILLED: "unjudged",
    ESCALATION_CAUSE_UNRECORDED: "unrecorded",
}

#: WHY this round escalated, one sentence per cause — spec §3's table (#1040)
ESCALATION_WHY: dict[str, str] = {
    ESCALATION_ROUNDS_SPENT:
        "the panel rejected this and had no round left: round {n} was its last "
        "(validation.max_rounds).",
    ESCALATION_USER_REWORK_CEILING:
        "the panel rejected this. Round {n} was the round after your own rejection, "
        "judged at the round budget's ceiling, so the rejection had no round left to "
        "send back — nobody abandoned it, and its asks are in the round below.",
    ESCALATION_REBINDS_SPENT:
        "the panel rejected a merge the OS asked for itself, {rebind_max} times running.",
    ESCALATION_BOUNCE_EXHAUSTED:
        "the submitter was sent back {bounce_limit} times running for changing nothing "
        "the panel asked about, and changed none of them. Nobody judged the latest "
        "submission.",
    ESCALATION_NOTHING_TO_JUDGE:
        "the submission changed no files and recorded no other durable effect, so no "
        "seat judged it.",
    ESCALATION_REPEAT_SUBMISSION:
        "the submission was identical to the preceding round, so the panel had nothing "
        "new to judge.",
    ESCALATION_NO_VERDICT: "the panel reached no verdict. Nobody has judged the work.",
    ESCALATION_VALIDATOR_UNREACHABLE:
        "the panel could not be reached, so nobody has judged the work.",
    ESCALATION_PROMPT_REFUSED:
        "the OS built a seat prompt over its character ceiling and never sent it, so "
        "nobody has judged the work.",
    ESCALATION_PANEL_KILLED:
        "the panel's seats were killed by a signal before they could judge this "
        "submission, so nobody judged the work.",
    ESCALATION_CAUSE_UNRECORDED:
        "this round was escalated before the OS recorded which condition fired; the "
        "reason on the round below is what the panel said.",
}

#: What `round {n} …` reads as per family, for `cli._readable_review`'s `where` clause
ESCALATION_VERB: dict[str, str] = {
    "handover": "was rejected and handed to you",
    "not_reviewed": "was not reviewed",
    "unjudged": "could not be judged",
    "unrecorded": "was escalated",
}

#: The WORK ORDER give-up notification, worded off the same verbs
ESCALATION_TITLE = "{unit} — round {n} {verb}"

#: `proof` on `CARRY_REFUSED_EVENT`, one value per condition
PROOF_CHAIN = "chain"
PROOF_PATCH_ID = "patch_id"
PROOF_FETCH = "fetch"
PROOF_READ = "read"


def record_base_health(store: ProjectStore, wo: dict[str, Any], *, red: bool,
                       base: str) -> bool:
    """Write down whether this pull request's base is broken"""
    from . import db, invariants as invariants_mod

    rows = store.events_of_kind(wo["id"], invariants_mod.PR_BASE_HEALTH_EVENT)
    if rows:
        last = db.from_json(rows[-1].get("payload"), {}) or {}
        if bool(last.get("red")) == red:
            return False
    elif not red:
        # NOTHING TO SAY
        return False
    store.add_event(wo["id"], invariants_mod.PR_BASE_HEALTH_EVENT,
                    {"red": red, "base": base, "pr_url": wo.get("pr_url")})
    return True


def base_heal_spent(store: ProjectStore, wo_id: str, base_sha: str) -> bool:
    """Has this pull request already been rebuilt against THIS base commit?"""
    from . import db, invariants as invariants_mod

    for kind in (invariants_mod.PR_BASE_UPDATED_EVENT,
                 invariants_mod.PR_BASE_UPDATE_FAILED_EVENT):
        for row in store.events_of_kind(wo_id, kind):
            if (db.from_json(row.get("payload"), {}) or {}).get("base_sha") == base_sha:
                return True
    return False


def record_base_update(store: ProjectStore, wo: dict[str, Any], *, base: str,
                       base_sha: str, head_before: str, head_after: str,
                       checks: tuple[str, ...], cause: str) -> None:
    """The OS healed this pull request itself"""
    from . import invariants as invariants_mod

    store.add_event(wo["id"], invariants_mod.PR_BASE_UPDATED_EVENT,
                    {"pr_url": wo.get("pr_url"), "base": base, "base_sha": base_sha,
                     "head_before": head_before, "head_after": head_after,
                     "checks": list(checks), "cause": cause})


def record_base_update_failed(store: ProjectStore, wo: dict[str, Any], *, base: str,
                              base_sha: str, reason: str, cause: str) -> None:
    """GitHub refused the update"""
    from . import invariants as invariants_mod

    store.add_event(wo["id"], invariants_mod.PR_BASE_UPDATE_FAILED_EVENT,
                    {"pr_url": wo.get("pr_url"), "base": base, "base_sha": base_sha,
                     "reason": reason, "cause": cause})


def catch_up_attempts(store: ProjectStore, wo_id: str, *, now: float = 0.0) -> int:
    """How often the OS has caught this pull request up within `CATCH_UP_WINDOW_SECONDS`"""
    from . import invariants as invariants_mod

    since = (now or db.now()) - CATCH_UP_WINDOW_SECONDS
    n = 0
    for row in store.events_of_kind(wo_id, invariants_mod.PR_BASE_UPDATED_EVENT):
        payload = db.from_json(row.get("payload"), {}) or {}
        if payload.get("cause") == BASE_UPDATE_BEHIND and float(row["ts"] or 0.0) >= since:
            n += 1
    return n


def catch_up_needed(pr: Any, *, repo: Path, base_tip: str = "") -> bool:
    """Is this pull request behind its base? (#837)"""
    from . import branchproof

    if getattr(pr, "behind", False):
        return True
    base_oid = base_tip or str(getattr(pr, "base_oid", "") or "")
    head = str(getattr(pr, "head_oid", "") or "")
    if not base_oid or not head:
        return False        # GitHub answered no base commit: nothing to be behind of
    return not branchproof.is_ancestor(repo, base_oid, head)


def _carry_round_onto(store: ProjectStore, wo: dict[str, Any], *, judged: str, head: str,
                      facts: Callable[[dict], tuple[str, dict] | None]) -> dict | None:
    """The guard both carries owe and the one write both make"""
    row = store.latest_validation_round(wo_id=wo["id"])
    if row is None or not judged or not head or head == judged:
        return None
    if ProjectStore.validated_head(row) != judged:
        return None
    built = facts(row)
    if built is None:
        return None
    reason, extra = built
    store.carry_round_head(int(row["id"]), head, reason)
    carried = {"round": int(row["round"]), "round_id": int(row["id"]),
               "judged_sha": judged, "carried_head_sha": head, "reason": reason, **extra}
    # AFTER the binding, so `rejudge_exhausted`'s last clause reads the carried head
    from . import invariants as invariants_mod

    fresh = store.get_work_order(wo["id"])
    lowered = bool(fresh["needs_attention"]) and not invariants_mod.true_blockers(
        store, fresh)
    if lowered:
        carried["attention_cleared"] = True
    store.add_event(wo["id"], HEAD_CARRIED_EVENT, carried)
    if lowered:
        store.clear_attention(wo["id"])
    return carried


def carry_validated_head(store: ProjectStore, wo: dict[str, Any], *, judged: str,
                         head_after: str, base: str, base_sha: str,
                         parents: tuple[str, ...]) -> dict | None:
    """Bind the panel's existing verdict to the commit the OS's own merge produced"""
    def facts(row: dict) -> tuple[str, dict] | None:
        if len(parents) != 2 or parents[0] != judged:
            log.info("%s: not carrying round %s onto %s — its parents are %s, not a merge "
                     "of %s with the base", wo["id"], row["round"], head_after[:10],
                     [p[:10] for p in parents] or "unreadable", judged[:10])
            return None
        return ((f"the OS merged `{base}` ({base_sha[:10]}) into this branch to clear a "
                 f"failure inherited from a red base — no authored content changed"),
                # THE PROOF, not a restatement of the claim: the commit's own parents as GitHub answered them
                {"cause": CARRY_BASE_HEAL, "base": base, "base_sha": base_sha,
                 "parents": list(parents), "merged_base_sha": parents[1]})

    return _carry_round_onto(store, wo, judged=judged, head=head_after, facts=facts)


def carry_merge_chain(store: ProjectStore, wo: dict[str, Any], *, judged: str, head: str,
                      chain: tuple[tuple[str, str, bool], ...], base: str, base_sha: str,
                      fingerprints: tuple[str, str]) -> dict | None:
    """Bind the panel's verdict to a head that is `judged` plus merges that added nothing (docs/superpowers/specs/2026-09-27-a-catch-up-with-main-costs-no-round.md §3.)"""
    before, after = fingerprints

    def facts(_row: dict) -> tuple[str, dict] | None:
        if not chain or chain[-1][0] != head:
            return None         # oldest-first: the newest commit walked IS the head
        if not before or not after or before != after:
            # Belt to the daemon's braces: a differing diff is authored content
            return None
        bases = [merged for _sha, merged, is_base in chain if is_base]
        branch = [merged for _sha, merged, is_base in chain if not is_base]
        # Two sentences and not one, because `merged_base_shas` must not lie: past Neo question 806 a walked...
        said = ([f"`{base}` was merged into this branch {len(bases)} time(s)"]
                if bases else [])
        if branch:
            said.append(f"{len(branch)} merge(s) brought in only commits {judged[:10]} "
                        f"already contained")
        # SAY WHAT IS TRUE, and it is a HASH comparison and not a comparison of the diff text (review round...
        return ((" and ".join(said) + f", and the pull request's own diff is unchanged "
                 f"down to its whitespace (diff hash {after[:12]}, line numbers aside) "
                 f"— no authored content changed"),
                # THE PROOF, not a restatement of the claim: the commits walked and what each of them merged in
                {"cause": CARRY_BASE_MERGE_CHAIN,
                 "chain": [sha for sha, _merged, _base in chain],
                 "merged_base_shas": bases, "merged_branch_shas": branch,
                 "base": base, "base_sha": base_sha, "patch_id": after})

    return _carry_round_onto(store, wo, judged=judged, head=head, facts=facts)


def carry_refusal_told(store: ProjectStore, wo_id: str, head_sha: str,
                       proof: str) -> bool:
    """Has this refusal already been written for this commit? §4's dedupe"""
    for row in store.events_of_kind(wo_id, CARRY_REFUSED_EVENT):
        payload = db.from_json(row.get("payload"), {}) or {}
        if (str(payload.get("head_sha") or "") == head_sha
                and str(payload.get("proof") or "") == proof):
            return True
    return False


def record_carry_refusal(store: ProjectStore, wo: dict[str, Any], *, judged: str,
                         head_sha: str, proof: str, detail: str,
                         chain: tuple[str, ...] = ()) -> bool:
    """Say on the record that the verdict was NOT carried, and which proof said so"""
    if not head_sha or carry_refusal_told(store, wo["id"], head_sha, proof):
        return False
    store.add_event(wo["id"], CARRY_REFUSED_EVENT,
                    {"judged_sha": judged, "head_sha": head_sha, "proof": proof,
                     "detail": detail, "chain": list(chain)})
    return True


def pr_repair_origin(store: ProjectStore, wo_id: str) -> str | None:
    """The status an OPEN repair episode took this work order out of, if any"""
    return store.pr_repair_origin(wo_id, tuple(r.name for r in PR_REPAIRS))


#: What a relaunched turn may put a work order back into, and the only status that needs it
RESUMABLE_ORIGINS = ("needs_review",)


def resumed_from(store: ProjectStore, wo_id: str, seq: int) -> str | None:
    """The status `Daemon.retry_paused_turns` took this work order out of for THIS turn (#259)"""
    events = store.events_of_kind(wo_id, "turn_resumed")
    if not events:
        return None
    payload = db.from_json(events[-1].get("payload"), {}) or {}
    if payload.get("seq") != seq:
        return None
    was = payload.get("was")
    return was if was in RESUMABLE_ORIGINS else None


def _awaiting_merge(wo: dict[str, Any]) -> bool:
    """True when this work order's ending is still a pull request nobody has merged"""
    return bool(wo.get("pr_url")) and wo.get("pr_state") not in ("MERGED", "CLOSED")


def stop_worker_session(wo: dict[str, Any], store: ProjectStore) -> dict[str, Any]:
    """Take the worker down with the work order, if anything of it is still running"""
    from . import claude_cli, worker_session

    killed = worker_session.cancel(store, wo["id"])
    if killed["stopped"]:
        return {"stopped": True, "pid": killed["pid"],
                "session_id": wo.get("session_id")}

    # Nothing of ours in flight
    if not wo.get("job_id"):
        return {"stopped": False, "reason": killed.get("reason", "no turn in flight")}
    if not claude_cli.available():
        return {"stopped": False, "reason": "claude CLI not available"}
    bg_id, reason = None, "no live session"
    try:
        for sess in claude_cli.list_background_sessions():
            if (wo.get("session_id") and sess.session_id == wo["session_id"]) or \
                    sess.name.startswith(f"[WO {wo['id']}]"):
                bg_id = sess.id
                break
    except claude_cli.ClaudeCliError as e:
        reason = f"could not list sessions: {e}"
    if bg_id is None:
        bg_id = wo.get("job_id")  # roster missed it; try the id we were handed at spawn
    if not bg_id:
        return {"stopped": False, "reason": reason}
    if claude_cli.stop_session(bg_id):
        return {"stopped": True, "bg_id": bg_id, "session_id": wo.get("session_id")}
    return {"stopped": False, "bg_id": bg_id, "reason": "`claude stop` failed"}


def respawn_worker(wo_id: str, project_name: str | None = None) -> dict[str, Any]:
    """Restart an order's background session process under the same id, sending nothing (docs/superpowers/specs/2026-10-22-workers-on-background-sessions.md)"""
    from . import claude_cli, worker_session

    _name, path, wo = find_work_order(wo_id, project_name)
    if not worker_session.is_background(wo) or not wo.get("bg_id"):
        raise OpsError(f"{wo_id} does not run on a background session — nothing to respawn")
    store = ProjectStore(path)
    try:
        if worker_session.busy(store, wo_id):
            # A restart under a running turn would end it wherever it had got to
            raise OpsError(f"{wo_id} has a turn in flight; respawn it once that settles")
        ok = claude_cli.respawn_bg(str(wo["bg_id"]))
        if ok:
            store.add_event(wo_id, "session_respawned", {"bg_id": wo["bg_id"],
                                                         "session_id": wo["session_id"]})
    finally:
        store.close()
    if not ok:
        raise OpsError(f"`claude respawn {wo['bg_id']}` failed")
    return {"wo_id": wo_id, "bg_id": wo["bg_id"], "session_id": wo["session_id"],
            "respawned": True}


def cancel(wo_id: str) -> dict[str, Any]:
    name, path, wo = find_work_order(wo_id)
    store = ProjectStore(path)
    try:
        stopped = stop_worker_session(wo, store)
        store.set_status(wo_id, "cancelled")
        store.clear_attention(wo_id)
        if stopped["stopped"]:
            store.add_event(wo_id, "session_stopped",
                            {**{k: v for k, v in stopped.items() if k != "stopped"},
                             "reason": "work order cancelled"})
    finally:
        store.close()
    out = {"project": name, "wo_id": wo_id, "status": "cancelled",
           "session_stopped": stopped["stopped"]}
    if not stopped["stopped"] and wo.get("session_id") and wo["status"] in OPEN_STATUSES:
        out["note"] = (f"the worker's session ({wo['session_id']}) could not be stopped "
                       f"({stopped.get('reason')}) — stop it from the agents view")
    return out


def ack_attention(wo_id: str | None = None, all_projects: bool = False,
                  project_name: str | None = None) -> dict[str, Any]:
    """Acknowledge attention flags — "I have seen this, stop showing it to me\""""
    if not wo_id and not all_projects:
        raise OpsError("give a work order id, or --all to acknowledge everything")

    if wo_id:
        name, path, _ = find_work_order(wo_id, project_name)
        targets = {name: path}
    else:
        targets = registered_project_paths()
        if project_name:
            if project_name not in targets:
                raise OpsError(f"project {project_name!r} not registered")
            targets = {project_name: targets[project_name]}

    acknowledged: list[str] = []
    skipped: list[dict[str, str]] = []
    for _name, path in targets.items():
        if not path.is_dir():
            continue
        store = ProjectStore(path)
        try:
            if wo_id:
                candidates = [store.get_work_order(wo_id)]
            else:
                candidates = [w for w in store.list_work_orders() if w["needs_attention"]]
            for wo in candidates:
                blockers = true_blockers(store, wo)
                refusal = ack_refusal(wo, blockers)
                if refusal is not None:
                    if wo_id:
                        raise OpsError(refusal)
                    # The BLOCKER, not the sentence: a sweep reports what it skipped over
                    skipped.append({"wo_id": wo["id"],
                                    "reason": ack_decision_blocker(blockers) or ""})
                    continue
                store.ack_attention(wo["id"], blockers)
                acknowledged.append(wo["id"])
        finally:
            store.close()
    # ...and the notifications raised WITH those flags are the same incident
    read = 0
    if acknowledged:
        central = CentralStore()
        try:
            read = sum(central.ack_paired_inbox(w) for w in acknowledged)
        finally:
            central.close()
    return {"acknowledged": acknowledged, "skipped": skipped, "inbox_read": read}


def ack_inbox(inbox_id: int | None = None) -> dict[str, Any]:
    """`jarvis inbox ack` — mark notifications read"""
    from .invariants import true_blockers

    central = CentralStore()
    try:
        if inbox_id is None:
            items = central.unacked_inbox()
        else:
            one = central.get_inbox(inbox_id)
            items = [one] if one and one["status"] != "acked" else []
        n = central.ack_inbox(inbox_id)
    finally:
        central.close()
    paths = registered_project_paths()
    lowered: list[str] = []
    kept: list[dict[str, str]] = []
    for it in items:
        # A standalone notification has nothing paired to lower
        if it.get("paired_reason") is None or not it.get("wo_id"):
            continue
        path = paths.get(it["project"])
        if path is None or not path.is_dir():
            continue
        store = ProjectStore(path)
        try:
            try:
                wo = store.get_work_order(it["wo_id"])
            except KeyError:
                continue
            # Only the SAME incident: a flag that has since moved on is a new thing to see
            if not wo["needs_attention"] or wo.get("attention_reason") != it["paired_reason"]:
                continue
            blockers = true_blockers(store, wo)
            if ack_refusal(wo, blockers) is not None:
                # A pending decision is never dismissed by reading a notification
                kept.append({"wo_id": wo["id"],
                             "reason": ack_decision_blocker(blockers) or ""})
                continue
            store.ack_attention(wo["id"], blockers)
            lowered.append(wo["id"])
        finally:
            store.close()
    return {"acked": n, "attention_lowered": lowered, "attention_kept": kept}


INBOX_LEVELS = ("critical", "warning", "info")
INBOX_SORTS = ("priority", "newest", "oldest")


def select_inbox(items: list[dict[str, Any]], *, level: str = "", project: str = "",
                 q: str = "", sort: str = "priority") -> list[dict[str, Any]]:
    """Filter and order unacked inbox rows; `priority` is critical first, newest first within a level"""
    words = q.lower().split()
    out = [it for it in items
           if (not level or it["level"] == level)
           and (not project or it["project"] == project)
           and all(w in f"{it['title']} {it.get('body') or ''} {it.get('wo_id') or ''}".lower()
                   for w in words)]
    out.sort(key=lambda it: it["ts"], reverse=sort != "oldest")
    if sort == "priority":
        rank = {lv: i for i, lv in enumerate(INBOX_LEVELS)}
        out.sort(key=lambda it: rank.get(it["level"], len(rank)))
    return out


def ack_os_flag(wo_id: str, project_name: str | None = None) -> dict[str, Any]:
    """Put down an attention flag the OS raised for ITSELF, acknowledging nothing"""
    from .invariants import true_blockers

    name, path, _ = find_work_order(wo_id, project_name)
    store = ProjectStore(path)
    try:
        wo = store.get_work_order(wo_id)
        blockers = true_blockers(store, wo)
        if blockers:
            # Only when it would actually change: `flag_attention` writes a timeline row every call
            if not wo["needs_attention"] or wo.get("attention_reason") != blockers[0]:
                store.flag_attention(wo_id, blockers[0])
        elif wo["needs_attention"]:
            store.lower_attention(wo_id)
    finally:
        store.close()
    return {"project": name, "wo_id": wo_id, "attention_reason": blockers[0] if blockers
            else None, "blockers": blockers}


def hide_work_order(wo_id: str, hidden: bool = True,
                    project_name: str | None = None) -> dict[str, Any]:
    """Hide a work order from listings, summaries and the attention list"""
    name, path, wo = find_work_order(wo_id, project_name)
    store = ProjectStore(path)
    try:
        store.set_hidden(wo_id, hidden)
    finally:
        store.close()
    return {"project": name, "wo_id": wo_id, "title": wo["title"],
            "hidden": bool(hidden)}


def unblock_work_order(wo_id: str, drop_all: bool = False,
                       project_name: str | None = None) -> dict[str, Any]:
    """Cut the dependency edges and the precondition rows holding a pending order back (docs/superpowers/specs/2026-10-15-a-precondition-is-a-row-not-prose.md)"""
    from . import invariants

    name, path, wo = find_work_order(wo_id, project_name)
    store = ProjectStore(path)
    try:
        blockers = store.unfinished_dependencies(wo_id)
        rows = store.open_preconditions(wo_id)
        if not blockers and not rows:
            raise OpsError(f"{wo_id} is not blocked by anything")
        cut = blockers if drop_all else invariants.dead_dependencies(store, wo)
        live, stranding = _split_preconditions(rows, store)
        clearable = stranding + (live if drop_all else [])
        if not cut and not clearable:
            raise OpsError(
                f"{wo_id} is waiting on work that is still live "
                f"({', '.join([d['id'] for d in blockers] + live)}), not stranded. "
                f"Pass --all to cut those edges anyway."
            )
        cleared = store.clear_preconditions(wo_id, clearable, by="user")
        remaining = store.drop_dependencies(wo_id, [d["id"] for d in cut])
        # The stranding was the blocker; with the edge gone the work order is ordinary pending again
        if not remaining:
            store.clear_attention(wo_id)
    finally:
        store.close()
    return {"project": name, "wo_id": wo_id, "title": wo["title"],
            "dropped": [d["id"] for d in cut], "still_blocked_by": remaining,
            "preconditions_cleared": cleared}


def _split_preconditions(rows: list[dict[str, Any]],
                         store: ProjectStore) -> tuple[list[str], list[str]]:
    """(live, stranding) refs — what the daemon can still clear, and what it never can"""
    from . import preconditions

    live: list[str] = []
    stranding: list[str] = []
    central = CentralStore()
    try:
        for row in rows:
            ref = str(row["ref"])
            target = preconditions.deployed_target(ref)
            if target:
                # docs/superpowers/specs/2026-10-22-a-deployed-precondition-clears-itself.md §2.3
                (live if _deployed_hold_is_live(store, target)
                 else stranding).append(ref)
                continue
            item = central.get_backlog(ref) if ref.startswith("bl-") else None
            # Unsettled, NOT just `open`: a `promoted` item is still one the daemon can clear later
            (live if item is not None
             and item["status"] not in SETTLED_BACKLOG_STATUSES
             else stranding).append(ref)
    finally:
        central.close()
    return live, stranding


def _deployed_hold_is_live(store: ProjectStore, target: str) -> bool:
    """Can the daemon still resolve this `deployed:` target to a commit it can test?"""
    if not target.startswith("wo-"):
        return True
    try:
        store.get_work_order(target)
    except KeyError:
        return False
    return True


def delete_work_order(wo_id: str, project_name: str | None = None) -> dict[str, Any]:
    """Erase a work order everywhere: project DB, central inbox/backlog, Neo's questions"""
    name, path, wo = find_work_order(wo_id, project_name)
    store = ProjectStore(path)
    try:
        stopped = stop_worker_session(wo, store)
        deleted = store.delete_work_order(wo_id)
    finally:
        store.close()
    if wo.get("transport") == "background" and wo.get("bg_id"):
        # The order's ONE session goes with it (`claude rm` keeps a worktree holding unpushed work)
        from . import claude_cli
        deleted["background_session"] = int(
            claude_cli.remove_bg(str(wo["bg_id"])) is not None)
    central = CentralStore()
    try:
        deleted.update(central.purge_work_order(wo_id))
    finally:
        central.close()
    from .neo_store import NeoStore
    neo = NeoStore()
    try:
        deleted["neo_questions"] = neo.purge_work_order(wo_id)
    finally:
        neo.close()
    out = {"project": name, "wo_id": wo_id, "title": wo["title"], "deleted": deleted,
           "session_stopped": stopped["stopped"]}
    if not stopped["stopped"] and wo["session_id"] and wo["status"] in OPEN_STATUSES:
        out["note"] = (f"the worker's session ({wo['session_id']}) could not be stopped "
                       f"({stopped.get('reason')}) — stop it from the agents view")
    return out


def _validates_on_review(store: ProjectStore, wo_id: str, cfg: Any) -> bool:
    """Should accepting this work order's assumptions open a validation round? (docs/superpowers/specs/2026-09-13-two-gates-not-a-chain.md §1)"""
    return (validation_applies(cfg, store.get_work_order(wo_id), store)
            and store.latest_validation_round(wo_id=wo_id) is None)


def exempt_from_validation(store: ProjectStore, wo: dict[str, Any]) -> bool:
    """Is this submission one no panel round may open over?"""
    from . import verdicts

    # THE KIND TABLE, not a kind name: a kind settled by its submission is never judged by a panel
    return (kind_contract(wo).route == SETTLE_IN_SESSION
            and verdicts.verdict_stored(store, wo))


def validation_applies(cfg: Any, wo: dict[str, Any], store: ProjectStore) -> bool:
    """Does a validation round open over THIS submission? (docs/superpowers/specs/2026-10-01-a-release-that-authored-files-is-judged-like-any-other.md §1)"""
    return (cfg is not None and cfg.enabled
            and not (release.is_release_order(wo) and not str(wo.get("pr_url") or ""))
            and not exempt_from_validation(store, wo))


def _land_after_acceptance(store: ProjectStore, path: Path, wo_id: str,
                           cfg: Any) -> str:
    """Where a work order goes once its assumptions are all accepted"""
    if _validates_on_review(store, wo_id, cfg):
        submit_for_validation(store, path, store.get_work_order(wo_id),
                              declared=declared_evidence(store, wo_id), cfg=cfg)
    fresh = store.get_work_order(wo_id)
    # THE ONE PLACE `pr_state` MAY BE READ
    if not _awaiting_merge(fresh):
        fresh = {**fresh, "pr_url": ""}
    # Told, not re-read: an exempt submission opened no round here either, release or investigator
    cleared = not validation_applies(cfg, store.get_work_order(wo_id), store)
    # The assumption gate has cleared
    return land_when_cleared(store, fresh, panel_cleared=cleared)


def accept_assumption(store: ProjectStore, project_path: Path, wo: dict[str, Any],
                      assumption: dict[str, Any], *, reason: str, model: str,
                      question_id: int, cfg: Any,
                      config_version_id: str | None = None) -> dict[str, Any]:
    """THE OS settling ONE assumption on the user's behalf (docs/superpowers/specs/2026-09-15-neo-decides-an-assumption.md §4.)"""
    wo_id = wo["id"]
    store.review_assumption(
        assumption["id"], "accepted",
        decided_by=ASSUMPTION_DECIDER_OS, reason=reason, model=model,
        config_version=(config_version_id if config_version_id is not None
                        else current_config_version()))
    store.add_event(wo_id, "autoreview_accepted", {
        "assumption_id": assumption["id"], "n": assumption.get("n"),
        "reason": reason, "model": model, "neo_question_id": question_id,
        "decided_by": ASSUMPTION_DECIDER_OS})
    pending = store.pending_assumptions(wo_id)
    status = str(wo.get("status") or "")
    if not pending:
        status = _land_after_acceptance(store, project_path, wo_id, cfg)
    return {"wo_id": wo_id, "assumption_id": assumption["id"],
            "n": assumption.get("n"), "status": status, "pending": len(pending),
            "settled": not pending}


#: The one transport that ships
OBJECTION_TRANSPORT = "queue"


def file_assumption_objection(store: ProjectStore, project_path: Path,
                              wo: dict[str, Any], assumption: dict[str, Any], *,
                              reason: str, model: str,
                              question_id: int | None) -> dict[str, Any]:
    """THE OS telling a RUNNING worker it disagrees with an assumption it just recorded (docs/superpowers/specs/2026-09-23-an-assumption-judged-while-the-worker-still-runs.md §6.1.)"""
    wo_id = wo["id"]
    envelope_id = bus.post(store, subject=bus.Subject(wo_id=wo_id),
                           from_role="reviewer", to_role="implementor",
                           payload=bus.AssumptionObjection(
                               assumption_n=int(assumption.get("n") or 0),
                               reason=reason, question_id=question_id))
    sent_ts = db.now()
    store.record_objection(assumption["id"], envelope_id=envelope_id,
                           transport=OBJECTION_TRANSPORT, sent_ts=sent_ts)
    store.add_event(wo_id, "autoreview_objected", {
        "assumption_id": assumption["id"], "n": assumption.get("n"),
        "transport": OBJECTION_TRANSPORT, "reason": reason, "model": model,
        "question_id": question_id, "envelope_id": envelope_id,
        "decided_by": ASSUMPTION_DECIDER_OS})
    return {"envelope_id": envelope_id, "transport": OBJECTION_TRANSPORT,
            "sent_ts": sent_ts}


def record_provisional_verdict(store: ProjectStore, wo: dict[str, Any],
                               assumption: dict[str, Any], *, verdict: str, reason: str,
                               model: str, stakes: str, question_id: int,
                               config_version_id: str | None = None) -> None:
    """THE OS's verdict on an assumption while the worker is still typing (docs/superpowers/specs/2026-09-23-an-assumption-judged-while-the-worker-still-runs.md §5.3.)"""
    store.record_provisional(
        assumption["id"], verdict=verdict, reason=reason, model=model, stakes=stakes,
        config_version=(config_version_id if config_version_id is not None
                        else current_config_version()))
    store.add_event(wo["id"], "autoreview_provisional", {
        "assumption_id": assumption["id"], "n": assumption.get("n"),
        "verdict": verdict, "reason": reason, "model": model, "stakes": stakes,
        "neo_question_id": question_id, "decided_by": ASSUMPTION_DECIDER_OS})


#: The event a user's 'false alarm' writes: a defect in the high-stakes net, never an authorisation
STAKES_FALSE_ALARM_EVENT = "autoreview_stakes_false_alarm"

#: The user's own words for a net that fired on nothing (wo-764454c5, wo-8fd40f1a)
FALSE_ALARM_RE = re.compile(r"\bfalse[ -]alarm", re.IGNORECASE)


def _record_stakes_false_alarms(store: ProjectStore, wo_id: str,
                                pending: list[dict[str, Any]], *, feedback: str,
                                net: str) -> frozenset[int]:
    """A user's 'false alarm' on a row the high-stakes net held is a CLASSIFIER DEFECT"""
    from . import autoreview

    held: dict[int, dict[str, Any]] = {}
    for event in store.events_of_kind(wo_id, "autoreview_held"):
        payload = db.from_json(event["payload"], {})
        if payload.get("code") == autoreview.HELD_HIGH_STAKES:
            held[int(payload.get("assumption_id") or 0)] = payload
    recorded = set()
    for a in pending:
        hold = held.get(int(a["id"]))
        if hold is None:
            continue
        content = str(a.get("content") or "")
        store.add_event(wo_id, STAKES_FALSE_ALARM_EVENT, {
            "assumption_id": a["id"], "n": a.get("n"), "net": net,
            "marker": autoreview.high_stakes_marker(
                content, tightened=net == "regex-tightened"),
            "hold_reason": str(hold.get("reason") or ""), "feedback": feedback})
        recorded.add(int(a["id"]))
    return frozenset(recorded)


def _teach_assumption_rules(project: str, wo: dict[str, Any], rows: list[dict[str, Any]],
                            accepted: bool, feedback: str, *,
                            false_alarms: frozenset[int] = frozenset(),
                            tightened: bool = False) -> list[int]:
    """Write the user's ruling as rule-shaped learnings — a no-reason one only once per class"""
    from . import neo as neo_mod
    from .neo_store import NeoStore

    if not rows:
        return []
    rules = neo_mod.learnings_from_assumption_review(
        wo, rows, accepted, feedback, false_alarms=false_alarms, tightened=tightened)
    neo = NeoStore()
    try:
        ids = []
        for rule, assumption_ids in rules:
            # A reasonless rule is the same sentence every time: writing it twice would crowd real rules out
            if not (feedback or "").strip() and neo.has_active_learning(rule, project):
                continue
            ids.append(int(neo.add_learning(
                rule, project=project, source="review",
                wo_id=str(wo.get("id") or ""), origin_kind="assumption",
                origin_id=",".join(str(i) for i in assumption_ids))["id"]))
        return ids
    finally:
        neo.close()


def review_work_order(wo_id: str, accept: bool = True, feedback: str = "",
                      false_alarm: bool = False) -> dict[str, Any]:
    """Accept (or reject) all pending assumptions and settle the work order"""
    name, path, wo = find_work_order(wo_id)
    cfg = validation_config(name)
    net = str(getattr(cfg, "stakes_classifier", "") or DEFAULT_STAKES_CLASSIFIER)
    store = ProjectStore(path)
    try:
        pending = [a for a in store.all_assumptions(wo_id) if a["status"] == "pending"]
        alarms: frozenset[int] = frozenset()
        if false_alarm or FALSE_ALARM_RE.search(feedback or ""):
            alarms = _record_stakes_false_alarms(store, wo_id, pending,
                                                 feedback=feedback, net=net)
        for a in pending:
            # STAMPED `user` RATHER THAN LEFT EMPTY
            store.review_assumption(a["id"], "accepted" if accept else "rejected",
                                    decided_by=ASSUMPTION_DECIDER_USER, reason=feedback)
        status = wo["status"]
        if wo["status"] == "needs_review":
            if accept:
                status = _land_after_acceptance(store, path, wo_id, cfg)
            elif not feedback:
                # With feedback the guidance is delivered below, so the work order is not waiting on the user
                store.flag_attention(wo_id, "assumptions rejected — send guidance with `jarvis wo send`")
        store.add_event(wo_id, "reviewed", {"accepted": accept, "count": len(pending),
                                            "feedback": feedback})
        park = store.pr_pending_open(wo_id)
    finally:
        store.close()

    out = {"project": name, "wo_id": wo_id, "reviewed": len(pending), "accepted": accept,
           "status": status}
    if alarms:
        out["false_alarms"] = len(alarms)
    learned = _teach_assumption_rules(name, wo, pending, accept, feedback,
                                      false_alarms=alarms,
                                      tightened=net == "regex-tightened")
    if learned:
        out["learning_id"], out["learning_ids"] = learned[0], learned
    # An accept that parked says what still waits (docs/superpowers/specs/2026-10-12-a-pushed-branch-with-no-pull-request-is-a-state.md §7)
    if park is not None:
        out["pr_pending"] = {k: park.get(k) or ""
                             for k in ("branch", "base", "compare_url")}
    if not feedback:
        return out

    # A rejection without guidance reaching the worker just strands it
    if not accept and wo["status"] in OPEN_STATUSES:
        try:
            out["delivered"] = send_message(wo_id, feedback, source="jarvis",
                                            project_name=name)
        except OpsError as e:
            out["delivery_error"] = str(e)
    return out


# -- feature orders

def create_feature_order(project_name: str, title: str, description: str = "",
                         origin: str = "jarvis",
                         backlog_id: str | None = None,
                         max_parallel: int | None = None,
                         budget_usd: float | None = None,
                         metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    """File the coarse ask"""
    paths = registered_project_paths()
    if project_name not in paths:
        raise OpsError(f"project {project_name!r} not registered "
                       f"(known: {sorted(paths)}). Run `jarvis start` first.")
    if max_parallel is not None and max_parallel < 1:
        raise OpsError("--max-parallel must be at least 1 (omit it for no cap)")
    if not (description or "").strip():
        # A work order can survive a bare title — a human reads it and fills the gaps
        raise OpsError(
            f"a feature order needs a description: the planner sees only this text, "
            f"and it is what the whole decomposition is built from. Use "
            f"`jarvis fo create {project_name} \"{title[:40]}\" -d \"...\"`."
        )
    store = ProjectStore(paths[project_name])
    try:
        return store.create_feature_order(
            title=title, description=description, origin=origin, backlog_id=backlog_id,
            max_parallel=max_parallel, metadata=metadata,
            # A FAMILY budget, so it takes the family default and never the per-work-order one
            budget_usd=(budget_usd if budget_usd is not None
                        else budget.feature_default_for(_spec_or_none(project_name))))
    finally:
        store.close()


def _spec_or_none(project_name: str) -> Any:
    """This project's catalog entry, or None when the catalog cannot be read"""
    try:
        return project_spec(resolve_catalog(), project_name)
    except (OpsError, CatalogError):
        return None


def feature_order_budget(fo_id: str, project_name: str | None = None) -> dict[str, Any]:
    """A feature's family budget, what the family has spent, and what is unreserved"""
    name, path, fo = find_feature_order(fo_id, project_name)
    store = ProjectStore(path)
    central = CentralStore()
    try:
        p = budget.pool(store, central, fo)
        spend = budget.feature_spent(store, central, fo)
        live = budget.feature_in_flight(store, fo)  # display only; see `budget.spent`
        # `budget.family` for the same reason as the set path's `exhausted_children`: the totals beside this...
        children = [
            {"wo_id": c["id"], "status": c["status"],
             "reserved_usd": c.get("budget_reserved_usd"),
             "spent_usd": budget.spent(store, central, c["id"]).total_usd}
            for c in budget.family(store, fo)
        ]
    finally:
        central.close()
        store.close()
    return {"project": name, "fo_id": fo_id, "title": fo["title"],
            "status": fo["status"], "budget_usd": fo.get("budget_usd"),
            "worker_usd": spend.worker_usd, "jarvis_usd": spend.jarvis_usd,
            "spent_usd": spend.total_usd,
            "in_flight_usd": live,
            "live_spent_usd": spend.total_usd + live,
            "reserved_usd": p.held_usd if p else None,
            "unreserved_usd": p.unreserved_usd if p else None,
            "children": children}


def find_feature_order(fo_id: str, project_name: str | None = None
                       ) -> tuple[str, Path, dict[str, Any]]:
    """Locate a feature order across all registered projects"""
    paths = registered_project_paths()
    if project_name and project_name not in paths:
        raise OpsError(f"project {project_name!r} not registered "
                       f"(known: {sorted(paths)})")
    candidates = {project_name: paths[project_name]} if project_name else paths
    for name, path in candidates.items():
        if not path.is_dir():
            continue
        store = ProjectStore(path)
        try:
            return name, path, store.get_feature_order(fo_id)
        except KeyError:
            continue
        finally:
            store.close()
    raise OpsError(f"feature order {fo_id!r} not found in any registered project")


#: Every id the OS mints that has a page, and the shapes a reader types for the rest
_REF_ID = re.compile(r"^(wo|fo|io|inv|kn|al|mv|bl)-[0-9a-f]{8}$")
_REF_NUMBERED = re.compile(r"^(q|gate-|issue-|#)?(\d+)$")


def resolve_ref(ref: str) -> str:
    """The dashboard page that shows this id — `/go/<id>` (docs/superpowers/specs/2026-10-22-a-console-you-can-move-through.md §1)"""
    ref = ref.strip()
    search = "/search?q=" + quote(ref)
    numbered = _REF_NUMBERED.match(ref.lower())
    if numbered:
        kind, n = numbered.groups()
        if kind == "gate-":
            return f"/gates#gate-{n}"
        if kind in ("issue-", "#"):
            return "/search?q=" + quote(f"#{n}")
        # a bare number is the one id a reader is shown without a prefix: a Neo question's
        return f"/neo/question/{n}"
    found = _REF_ID.match(ref.lower())
    if not found:
        return search
    rid, prefix = found.group(0), found.group(1)
    try:
        if prefix == "wo":
            name, _, _ = find_work_order(rid)
            return order_path(name, rid)
        if prefix in ("fo", "io", "inv"):
            name, _, _ = find_feature_order(rid)
            return order_path(name, rid)
        if prefix == "al":
            name, _, _ = _find_alarm(rid)
            return f"/alarms/{name}/{rid}"
    except OpsError:
        return search
    # Listed on one fleet-wide page each, and addressed there by their own id
    return {"kn": "/knowledge", "mv": "/morpheus", "bl": "/backlog"}[prefix] + f"#{rid}"


def worker_prompts(wo_id: str, project_name: str | None = None) -> dict[str, Any]:
    """Every prompt the OS sent this order's conversation, turn by turn — `wo_turns.prompt` verbatim"""
    name, path, wo = find_work_order(wo_id, project_name)
    store = ProjectStore(path)
    try:
        turns = store.list_turns(wo_id, limit=10_000)
    finally:
        store.close()
    return {"project": name, "wo": wo,
            "turns": [{"seq": t["seq"], "kind": t["kind"], "state": t["state"],
                       "started_at": t["started_at"], "msg_id": t["msg_id"],
                       "prompt": t["prompt"], "chars": len(t["prompt"] or "")}
                      for t in turns]}


#: The placeholder a prompt preview is composed for
PREVIEW_WO = {"id": "wo-00000000", "title": "(the title of a new work order)",
              "description": "(its description — the brief the user or Jarvis wrote)"}


def project_prompt_preview(name: str) -> dict[str, Any]:
    """What a NEW work order on this project would be told today, and the OPERATION.md beside it"""
    from .dispatch import build_worker_prompt, knowledge_for

    catalog = resolve_catalog()
    spec = project_spec(catalog, name)
    central = CentralStore()
    try:
        knowledge = knowledge_for(central, spec, PREVIEW_WO, catalog.os)
    finally:
        central.close()
    operation = spec.path / "OPERATION.md"
    return {"project": name, "path": str(spec.path), "wo": PREVIEW_WO,
            "prompt": build_worker_prompt(dict(PREVIEW_WO), spec, knowledge),
            "operation_path": str(operation),
            "operation": operation.read_text() if operation.is_file() else None}


def feature_progress(store: ProjectStore, fo: dict[str, Any]) -> dict[str, Any]:
    """How far along this family is, from the children its kind exists to run"""
    children = store.feature_children(
        fo["id"], kinds=FAMILY_CHILD_KINDS[fo.get("kind") or "feature"])
    done = sum(1 for c in children if c["status"] == "completed")
    return {
        "children": len(children),
        "done": done,
        "needs_attention": sum(1 for c in children if c["needs_attention"]),
        "running": sum(1 for c in children
                       if c["status"] in ("dispatching", "running", "waiting_input")),
        "awaiting_merge": sum(1 for c in children if c["status"] == "waiting_pr_merge"),
        "failed": sum(1 for c in children if c["status"] in ("failed", "cancelled")),
        "label": f"{done}/{len(children)} done" if children else "no children yet",
    }


def list_feature_orders(project_name: str | None = None,
                        include_settled: bool = False,
                        kind: str = "feature") -> list[dict[str, Any]]:
    paths = registered_project_paths()
    if project_name:
        if project_name not in paths:
            raise OpsError(f"project {project_name!r} not registered")
        paths = {project_name: paths[project_name]}
    out = []
    for name, path in sorted(paths.items()):
        if not path.is_dir():
            continue
        store = ProjectStore(path)
        try:
            statuses = None if include_settled else FO_OPEN_STATUSES
            rows = store.list_feature_orders(statuses=statuses, kind=kind)
            if not include_settled:
                # THE DEFAULT LISTING IS THE OPEN ONES PLUS ANY FLAGGED ROW WHATEVER ITS STATUS (§6 of... (docs/superpowers/specs/2026-10-01-a-family-capped-raise-must-say-so.md)
                seen = {fo["id"] for fo in rows}
                rows += [fo for fo in store.flagged_feature_orders()
                         if fo["id"] not in seen
                         and (kind is None or (fo.get("kind") or "feature") == kind)]
                rows.sort(key=lambda fo: fo["created_at"], reverse=True)
            for fo in rows:
                out.append({"project": name, **fo,
                            "status_label": feature_status_label(kind, fo["status"]),
                            "progress": feature_progress(store, fo)})
        finally:
            store.close()
    return out


# -- improvement orders

#: `feature_orders.metadata` key: the `--ref` strings exactly as the user typed them
EVIDENCE_REFS_KEY = "evidence_refs"


def evidence_refs(fo: dict[str, Any]) -> list[str]:
    """The evidence an improvement order was filed with, off one row"""
    metadata = db.from_json(fo.get("metadata"), {}) or {}
    return list(metadata.get(EVIDENCE_REFS_KEY) or [])

#: `work_orders.metadata`/`feature_orders.metadata` key on an order FILED FROM a finding: the...
ORIGIN_IO_KEY = "origin_io"


def _require_kind(fo: dict[str, Any], kind: str, verb: str) -> None:
    """Refuse a row of the wrong kind, naming the command that WOULD work"""
    actual = fo.get("kind") or "feature"
    if actual == kind:
        return
    # A TABLE, not a derivation from two kinds: a third kind read as "a feature order" is a message that... (docs/superpowers/specs/2026-09-27-investigation-orders.md)
    other = _KIND_PHRASES.get(actual, "a feature order")
    raise OpsError(f"{fo['id']} is {other}, not {'an' if kind[0] in 'aeiou' else 'a'} "
                   f"{kind} order — use `{verb} {fo['id']}` instead")


#: How each `feature_orders.kind` reads in a refusal
_KIND_PHRASES = {
    "feature": "a feature order",
    "improvement": "an improvement order",
    "investigation": "an investigation order",
}

#: Per kind: how the FAMILY reads in prose, and the command that raises its budget (docs/superpowers/specs/2026-10-01-a-family-capped-raise-must-say-so.md)
_KIND_FAMILY = {
    "feature": ("feature", "jarvis fo budget"),
    "improvement": ("improvement order", "jarvis io budget"),
    "investigation": ("investigation", "jarvis investigate budget"),
}


#: Per kind: the verb that SHOWS one (#997)
_KIND_SHOW_VERBS = {
    "feature": "jarvis fo show",
    "improvement": "jarvis io show",
    "investigation": "jarvis investigate show",
}


def show_verb(kind: str | None) -> str:
    """The command that shows a `feature_orders` row of this kind"""
    return _KIND_SHOW_VERBS.get(kind or "feature", _KIND_SHOW_VERBS["feature"])


def family_prose(kind: str | None) -> tuple[str, str]:
    """`(what the family is called, the command that raises its budget)` for one kind"""
    return _KIND_FAMILY.get(kind or "feature", _KIND_FAMILY["feature"])


#: Said to a WORKER whose improvement order the steward did not dispatch (Q2134)
IO_STEWARD_REFUSED = (
    "the steward did not dispatch this improvement order ({outcome}): {reason} "
    "(decision #{decision_id}). Nothing was created and nothing is spending. It is on "
    "the record: `jarvis steward decisions`"
)
#: Said to a WORKER whose improvement order the steward QUEUED: kept, and filed later (#61)
IO_STEWARD_QUEUED = (
    "the steward did not dispatch this improvement order yet (queue): {reason} "
    "(decision #{decision_id}). Nothing is spending now; the observation is kept whole and "
    "the OS files it by itself once the evolution ledger has room. Do not file it again. "
    "It is on the record: `jarvis steward decisions`"
)
#: Said to a WORKER whose io could not be decided at all: no decision is not a yes (#60)
IO_STEWARD_UNREADABLE = (
    "the steward could not read the catalog, so it cannot decide this improvement order "
    "({error}). Nothing was created. A worker's io waits for a readable catalog; ask the "
    "user to run `jarvis doctor`, or put the observation in your summary instead"
)
#: `decide` said dispatch and the master switch said no (`os.autonomy.self_dispatch`)
IO_SELF_DISPATCH_OFF = ("`os.autonomy.self_dispatch` is off, so the OS files nothing "
                        "nobody typed")


def _gate_improvement_order(project_name: str, title: str, refs: Sequence[str],
                            store: ProjectStore, *, description: str = "",
                            origin: str = "jarvis",
                            budget_usd: float | None = None) -> Any:
    """The steward's call on an io a WORKER filed, or None when the user typed it (Q2134)"""
    from . import steward

    parent_wo_id = os.environ.get("JARVIS_WO_ID") or ""
    if not parent_wo_id:
        return None  # spec §3.2 decision 1 rule 1: the user typed it
    try:
        catalog = resolve_catalog()
    except (OpsError, CatalogError, OSError, ValueError) as e:
        # A FAILURE IS NEVER A VERDICT (kn-40db1828), and work nobody typed needs a yes (#60)
        log.warning("steward: catalog unreadable; %s io create refused", project_name)
        raise OpsError(IO_STEWARD_UNREADABLE.format(error=str(e)[:200])) from e
    decision = steward.gate(
        steward.Candidate("worker_io", "major", project_name, title,
                          evidence=tuple(refs), parent_wo_id=parent_wo_id,
                          refs=len(refs),
                          # Everything `reoffer_improvement_order` needs to file it later
                          payload={"title": title, "description": description,
                                   "refs": list(refs), "origin": origin,
                                   "budget_usd": budget_usd}),
        catalog=catalog, store=store)
    if not decision.allowed and decision.outcome == "queue" and not decision.duplicate_of:
        raise OpsError(IO_STEWARD_QUEUED.format(reason=decision.reason,
                                                decision_id=decision.decision_id))
    if not decision.allowed:
        raise OpsError(IO_STEWARD_REFUSED.format(
            outcome=decision.outcome, decision_id=decision.decision_id,
            reason=(IO_SELF_DISPATCH_OFF if decision.would_dispatch
                    else decision.reason)))
    return decision


def create_improvement_order(project_name: str, title: str, description: str = "",
                             refs: Sequence[str] = (),
                             budget_usd: float | None = None,
                             origin: str = "jarvis") -> dict[str, Any]:
    """File an observation for an analyst to investigate"""
    paths = registered_project_paths()
    if project_name not in paths:
        raise OpsError(f"project {project_name!r} not registered "
                       f"(known: {sorted(paths)}). Run `jarvis start` first.")
    refs = [r for r in (s.strip() for s in refs) if r]
    if not (description or "").strip():
        raise OpsError(
            f"an improvement order needs an observation: the analyst's first reader is "
            f"a fresh session with no memory of the conversation that produced it. Use "
            f"`jarvis io create {project_name} \"{title[:40]}\" -d \"...\"`."
        )
    if not refs:
        raise OpsError(
            f"an improvement order with no evidence is a request for an opinion. Name "
            f"what you saw — a wo-/fo-/io-/al- id, #<issue>, a URL, or free text: "
            f"`jarvis io create {project_name} \"{title[:40]}\" -d \"...\" --ref <id>`."
        )
    store = ProjectStore(paths[project_name])
    try:
        decision = _gate_improvement_order(project_name, title, refs, store,
                                           description=description, origin=origin,
                                           budget_usd=budget_usd)
        return _file_improvement_order(store, project_name, title, description, refs,
                                       budget_usd, origin, decision)
    finally:
        store.close()


def _file_improvement_order(store: ProjectStore, project_name: str, title: str,
                            description: str, refs: Sequence[str],
                            budget_usd: float | None, origin: str,
                            decision: Any) -> dict[str, Any]:
    """Create the io — now, or when the steward re-offers a queued one (#61)"""
    from . import steward

    metadata: dict[str, Any] = {EVIDENCE_REFS_KEY: list(refs)}
    if decision is not None:
        metadata = steward.stamped(metadata, decision)
        if decision.budget_usd is not None:
            budget_usd = decision.budget_usd
    io = store.create_feature_order(
        title=title, description=description, origin=origin,
        kind="improvement",
        metadata=metadata,
        # The family here is the order plus its analyst
        budget_usd=(budget_usd if budget_usd is not None
                    else budget.feature_default_for(_spec_or_none(project_name))))
    if decision is not None:
        steward.linked(decision, io["id"], store=store)
    return io


def reoffer_improvement_order(row: Mapping[str, Any], candidate: Any,
                              decision: Any) -> str:
    """File a worker's queued io the steward now has room for (`steward.drain`, #61)"""
    held = candidate.payload or {}
    paths = registered_project_paths()
    if candidate.project not in paths:
        raise OpsError(f"project {candidate.project!r} is not registered")
    store = ProjectStore(paths[candidate.project])
    try:
        io = _file_improvement_order(
            store, candidate.project, candidate.subject or str(held.get("title") or ""),
            str(held.get("description") or ""), list(held.get("refs") or ()),
            held.get("budget_usd"), str(held.get("origin") or "jarvis"), decision)
    finally:
        store.close()
    # `linked` already ran with the store at hand; `drain` linking again is a no-op
    return str(io["id"])


def file_neo_cleanup(store: ProjectStore, rider: Mapping[str, Any],
                     decision: Any) -> dict[str, Any]:
    """The pre-approved ledger cleanup Neo asked for — now, or when the steward re-offers it"""
    from . import steward
    from .project_store import PRE_APPROVED_KEY

    question_id, from_wo = rider["question_id"], rider["wo_id"]
    description = "\n\n".join(filter(None, [
        rider.get("description") or "",
        f"Neo filed this while answering question {question_id} on {from_wo}. "
        f"The correction is ALREADY APPROVED — make it. The stores are append-only, "
        f"so the remedy is to APPEND an entry that supersedes the wrong one "
        f"(`jarvis learn add` for the knowledge base, `jarvis neo learn` for Neo's "
        f"own learnings), naming what it replaces and why.",
    ]))
    wo = store.create_work_order(
        title=rider["title"], description=description, origin="neo",
        metadata=steward.stamped({PRE_APPROVED_KEY: {
            "by": "neo",
            "scope": "correcting the recorded ledger entries this work order names",
            "neo_question_id": question_id,
            "from_wo": from_wo,
        }}, decision), budget_usd=decision.budget_usd,
    )
    steward.linked(decision, wo["id"], store=store)
    try:
        store.add_event(from_wo, "neo_dispatched",
                        {"neo_question_id": question_id, "cleanup_wo_id": wo["id"]})
    except Exception:  # noqa: BLE001 — the order it came from may be gone by a re-offer
        log.warning("neo cleanup %s filed; %s is gone, so no event there", wo["id"],
                    from_wo)
    log.info("neo dispatched pre-approved cleanup %s from %s", wo["id"], from_wo)
    return wo


def reoffer_neo_cleanup(row: Mapping[str, Any], candidate: Any, decision: Any) -> str:
    """File a queued Neo cleanup the steward now has room for (`steward.drain`)"""
    rider = candidate.payload or {}
    if not rider.get("title"):
        raise OpsError(f"steward decision #{row['id']} kept no cleanup to file")
    paths = registered_project_paths()
    if candidate.project not in paths:
        raise OpsError(f"project {candidate.project!r} is not registered")
    store = ProjectStore(paths[candidate.project])
    try:
        return str(file_neo_cleanup(store, rider, decision)["id"])
    finally:
        store.close()


def steward_queue_report(limit: int = 200) -> dict[str, Any]:
    """The dashboard's `/steward`: queue counts first, then each queued decision"""
    from . import steward

    central = CentralStore()
    try:
        counts = central.steward_queue_counts()
        rows = central.steward_queue_rows(limit)
        asks = central.steward_asks(db.now() - steward.WINDOW_SECONDS)
    finally:
        central.close()
    keys = ("id", "ts", "project", "origin_kind", "severity", "subject", "rule", "reason",
            "wo_id", "queue_state", "queue_note", "queue_ts")
    items = [{k: r[k] for k in keys} for r in rows]
    return {"asks": [{**{k: r[k] for k in keys}, "parent_wo_id": r["parent_wo_id"],
                      "morpheus_item": (steward._candidate_of(r).get("payload") or {}).get(
                          "morpheus_item", "")} for r in asks],
            "counts": {s: counts.get(s, 0) for s in ("waiting", "reoffered", "expired")},
            "line": steward.queue_line(counts),
            "waiting": [i for i in items if i["queue_state"] == "waiting"],
            "settled": [i for i in items if i["queue_state"] != "waiting"]}


def steward_dispatchers() -> dict[str, Any]:
    """How `steward.drain` dispatches a queued decision, per origin kind (#1181)"""
    from . import remedies

    return {"panel": reoffer_panel_follow_up, "worker_io": reoffer_improvement_order,
            "remedy": remedies.reoffer_file_work_order, "neo_rider": reoffer_neo_cleanup,
            "io_finding": reoffer_io_finding}


def list_improvement_orders(project_name: str | None = None,
                            include_settled: bool = False) -> list[dict[str, Any]]:
    """`jarvis io list`"""
    return list_feature_orders(project_name, include_settled=include_settled,
                               kind="improvement")


def show_improvement_order(io_id: str, project_name: str | None = None) -> dict[str, Any]:
    """`jarvis io show` — COUNTS FIRST, then the observation and the evidence"""
    name, path, fo = find_feature_order(io_id, project_name)
    _require_kind(fo, "improvement", "jarvis fo show")
    # `plan` is NULL until the analyst reports, which is the NORMAL state in this section
    report = db.from_json(fo.get("plan"), None) or {}
    findings = report.get("findings") or [] if isinstance(report, dict) else []
    by_decision = {"accepted": 0, "rejected": 0, "pending": 0}
    for f in findings:
        status = (f or {}).get("status") or "pending" if isinstance(f, dict) else "pending"
        by_decision[status if status in by_decision else "pending"] += 1
    from . import findings as findings_mod

    # Of the pending ones, those Neo is still deciding: they owe the user nothing yet
    with_neo = sum(1 for f in findings if isinstance(f, dict) and findings_mod.with_neo(f))
    metadata = db.from_json(fo.get("metadata"), {}) or {}
    store = ProjectStore(path)
    try:
        analyst = None
        if fo.get("plan_wo_id"):
            try:
                a = store.get_work_order(fo["plan_wo_id"])
                analyst = {k: a[k] for k in ("id", "title", "status", "result_summary")}
            except KeyError:
                analyst = None  # deleted out from under it; the link was released
        alarms = store.alarms_for_feature(io_id)
    finally:
        store.close()
    return {
        "project": name, **fo,
        "findings": len(findings),
        "by_decision": by_decision,
        "with_neo": with_neo,
        "report": report if isinstance(report, dict) else {},
        "filed_orders": _filed_orders(findings),
        "observation": fo["description"],
        "evidence_refs": evidence_refs(fo),
        "analyst": analyst,
        "status_label": feature_status_label("improvement", fo["status"]),
        "alarms": alarms,
    }


def _filed_orders(findings: list[Any]) -> dict[str, list[dict[str, Any]]]:
    """Every order each accepted finding filed, with its CURRENT status (§5.3.1)"""
    out: dict[str, list[dict[str, Any]]] = {}
    for finding in findings:
        if not isinstance(finding, dict):
            continue
        rows = []
        for order in finding.get("created_orders") or []:
            status = "deleted"
            try:
                if order.get("type") == "feature":
                    _n, _p, row = find_feature_order(order["id"])
                else:
                    _n, _p, row = find_work_order(order["id"])
                status = row["status"]
            except OpsError:
                pass
            rows.append({"id": order.get("id"), "type": order.get("type"),
                         "title": order.get("title"), "project": order.get("project"),
                         "status": status})
        if rows:
            out[finding.get("key", "")] = rows
    return out


def cancel_improvement_order(io_id: str, project_name: str | None = None
                             ) -> dict[str, Any]:
    """`jarvis io cancel`"""
    _, _, fo = find_feature_order(io_id, project_name)
    _require_kind(fo, "improvement", "jarvis fo cancel")
    return cancel_feature_order(io_id, project_name)


def improvement_order_budget(io_id: str, project_name: str | None = None
                             ) -> dict[str, Any]:
    """`jarvis io budget`, read-only (docs/superpowers/specs/2026-10-01-a-family-capped-raise-must-say-so.md)"""
    _, _, fo = find_feature_order(io_id, project_name)
    _require_kind(fo, "improvement", "jarvis fo budget")
    return feature_order_budget(io_id, project_name)


def set_improvement_budget(io_id: str, amount: float | None,
                           project_name: str | None = None) -> dict[str, Any]:
    """`jarvis io budget <amount>`"""
    _, _, fo = find_feature_order(io_id, project_name)
    _require_kind(fo, "improvement", "jarvis fo budget")
    return set_feature_budget(io_id, amount, project_name)


def submit_findings(io_id: str, doc: Any,
                    project_name: str | None = None) -> dict[str, Any]:
    """(Analysts) hand back the findings report"""
    from . import findings

    name, path, fo = find_feature_order(io_id, project_name)
    _require_kind(fo, "improvement", "jarvis fo plan")
    if fo["status"] not in ("planning", "plan_review"):
        raise OpsError(
            f"{io_id} is {fo['status']}, so it is not waiting for a report "
            f"(a report can be submitted while it is `planning`, or resubmitted while "
            f"it is `plan_review`)"
        )
    try:
        report = findings.parse_report(doc)
    except findings.FindingsError as e:
        raise OpsError(
            f"the report was not accepted, and nothing was stored. Fix all of these and "
            f"resubmit:\n  - " + "\n  - ".join(e.problems)
        ) from e

    for finding in report["findings"]:
        finding["status"] = "pending"

    # A resubmission replaces the findings Neo was asked about, so those questions are moot
    previous = db.from_json(fo.get("plan"), None)
    if isinstance(previous, dict):
        _supersede_finding_questions(
            previous.get("findings") or [], f"SUPERSEDED — {io_id}'s report was resubmitted",
            "the analyst replaced the report this finding belonged to")
    neo_review = findings_reviewed_by_neo()
    unasked = _ask_findings_review(name, fo, report) if neo_review else []

    store = ProjectStore(path)
    try:
        # The WHOLE document, replacing whatever was there: a resubmission discards every decision already...
        store.update_feature_order(io_id, plan=db.to_json(report))
        store.set_feature_status(io_id, "plan_review")
        # EXACTLY ONE attention item, raised HERE — unless every finding is Neo's to decide
        if neo_review and not unasked:
            store.clear_feature_attention(io_id)
        else:
            store.flag_feature_attention(io_id, findings.review_headline(fo, report))
        analyst_open = False
        if fo.get("plan_wo_id"):
            store.add_event(fo["plan_wo_id"], "findings_submitted", {
                "improvement_order": io_id, "findings": len(report["findings"]),
                "neo_questions": len(report["findings"]) - len(unasked) if neo_review else 0,
            })
            try:
                analyst = store.get_work_order(fo["plan_wo_id"])
                analyst_open = analyst["status"] in OPEN_STATUSES
            except KeyError:
                analyst_open = False  # deleted out from under it; the link was released
    finally:
        store.close()

    out: dict[str, Any] = {
        "project": name, "io_id": io_id, "status": "plan_review",
        "findings": len(report["findings"]),
        "note": ("queued for Neo's decision, finding by finding" if neo_review and not unasked
                 else "queued for the user's decision")
                + " — end your turn. If it is sent back, the reason arrives as your next "
                  "user turn and you revise from this session.",
    }
    if analyst_open:
        # Only while it is still open; a refusal AFTER the report is stored is not a rejection
        try:
            out["analyst"] = _settle_submitter(
                path, fo["plan_wo_id"],
                f"submitted findings for {io_id}: {len(report['findings'])} findings",
                "findings_submitted_settled", {"improvement_order": io_id})
        except OpsError as e:
            out["warning"] = submitter_unsettled("report", io_id, fo["plan_wo_id"], e)
    return out


def review_findings(io_id: str, accept: Sequence[str] = (),
                    reject: dict[str, str] | None = None, feedback: str = "",
                    decided_by: str = "user", accept_all: bool = False,
                    project_name: str | None = None, *,
                    steward_decision: Any = None,
                    file_orders: bool = True) -> dict[str, Any]:
    """Decide a findings report, finding by finding"""
    from . import findings
    from .neo_store import NeoStore

    rejections = dict(reject or {})
    accepted_keys = list(accept)
    name, path, fo = find_feature_order(io_id, project_name)
    _require_kind(fo, "improvement", "jarvis fo approve")
    if fo["status"] != "plan_review":
        raise OpsError(
            f"{io_id} is {fo['status']}, not awaiting a findings review — "
            f"`jarvis io show {io_id}` for where it stands"
        )
    report = db.from_json(fo.get("plan"), None)
    if not isinstance(report, dict) or not report.get("findings"):
        raise OpsError(
            f"{io_id} has no stored findings report to review — its analyst has not "
            f"reported yet. `jarvis io show {io_id}`."
        )
    by_key = {f["key"]: f for f in report["findings"]}

    if accept_all and rejections:
        raise OpsError(
            "--accept-all cannot be combined with --reject: one reason covering a "
            "blanket rejection is a rejection nobody can learn from. Name each finding "
            "with --accept/--reject instead."
        )
    if accept_all:
        accepted_keys = [k for k, f in by_key.items()
                         if (f.get("status") or "pending") == "pending"
                         or f.get("filing_error")]
    unknown = [k for k in (*accepted_keys, *rejections) if k not in by_key]
    if unknown:
        raise OpsError(
            f"no finding {unknown[0]!r} on {io_id} — the keys are: "
            f"{', '.join(by_key)}"
        )
    both = [k for k in accepted_keys if k in rejections]
    if both:
        raise OpsError(f"{both[0]!r} is in both --accept and --reject — decide it once")
    blank = [k for k, reason in rejections.items() if not (reason or "").strip()]
    if blank:
        raise OpsError(
            f"rejecting {blank[0]!r} needs --feedback: the reason is the entire teaching "
            f"signal, and Neo learns nothing from a refusal with no argument"
        )
    if not accepted_keys and not rejections:
        raise OpsError(
            f"name at least one finding: `jarvis io review {io_id} --accept <key>` or "
            f"`--reject <key> --feedback \"why\"` (the keys are: {', '.join(by_key)})"
        )
    for key in (*accepted_keys, *rejections):
        f = by_key[key]
        if (f.get("status") or "pending") == "pending":
            continue
        # The ONE re-decision allowed: a finding whose decision stands but whose orders did not file (§5.2)
        if key in accepted_keys and (f.get("filing_error") or f.get("orders_held")):
            continue
        raise OpsError(
            f"{key!r} is already {f['status']} on {io_id} — a decision is taken once. "
            f"`jarvis io show {io_id}` for what was decided."
        )

    # Taken BEFORE the decisions below, which end `with_neo` for every finding they decide
    neo_was_deciding = [dict(by_key[k]) for k in (*accepted_keys, *rejections)
                        if findings.with_neo(by_key[k])]
    created: list[dict[str, Any]] = []
    learnings: list[str] = []
    errors: list[dict[str, str]] = []
    for key in accepted_keys:
        f = by_key[key]
        if not f.get("knowledge_id"):
            # Through `learn_add` and never `CentralStore.add_knowledge`
            entry = learn_add(findings.knowledge_text(f), project=name,
                              topic="os-failure-mode", tags=io_id,
                              wo_id=fo.get("plan_wo_id") or "")
            f["knowledge_id"] = entry["id"]
            learnings.append(entry["id"])
        if file_orders:
            made, error = _file_finding_orders(name, io_id, f, steward_decision)
            created += made
            if error:
                errors.append({"key": key, "error": error})
            if decided_by == "user":
                # The user's yes files it now; a steward queue row still waiting finds nothing left to file
                f.pop("orders_held", None)
        elif f.get("proposed_orders"):
            f["orders_held"] = int(getattr(steward_decision, "decision_id", 0) or 0) or True
        # The DECISION is written once
        if f.get("status") != "accepted":
            f.update(status="accepted", decided_by=decided_by, decided_at=db.now(),
                     feedback=feedback)

    for key, reason in rejections.items():
        by_key[key].update(status="rejected", decided_by=decided_by, decided_at=db.now(),
                           feedback=reason)
    if rejections and decided_by == "user":
        # Only the user's refusal teaches Neo: Neo learning from its own rulings would be a loop
        neo = NeoStore()
        try:
            for key, reason in rejections.items():
                neo.add_learning(
                    findings.rejection_learning(fo, by_key[key], reason, decided_by),
                    project=name, source="review",
                    wo_id=fo.get("plan_wo_id") or "", origin_kind="finding",
                    origin_id=f"{io_id}:{key}")
        finally:
            neo.close()
    if decided_by == "user":
        # The user got there first: the question Neo still holds about it is moot
        _supersede_finding_questions(
            neo_was_deciding, "SUPERSEDED — the user decided this finding",
            f"decided with `jarvis io review {io_id}`")

    pending = [f for f in report["findings"]
               if (f.get("status") or "pending") == "pending"]
    stuck = [f for f in report["findings"] if f.get("filing_error")]
    status = "completed" if not pending and not stuck else "plan_review"
    reason = ""
    # Only what waits on the USER raises the flag; a finding Neo is still deciding owes them nothing
    owed = [f for f in pending if not findings.with_neo(f)]

    store = ProjectStore(path)
    try:
        store.update_feature_order(io_id, plan=db.to_json(report))
        # THE FLAG MOVES ONLY HERE, at the transition, and nothing re-derives it on a tick
        if status == "completed":
            store.set_feature_status(io_id, "completed")
            store.clear_feature_attention(io_id)
        elif stuck:
            reason = (
                f"{io_id}: {len(stuck)} accepted finding(s) could not be filed — "
                f"{stuck[0]['key']}: {stuck[0]['filing_error']} — retry with "
                f"`jarvis io review {io_id} --accept {stuck[0]['key']}`"
            )
            store.flag_feature_attention(io_id, reason)
        elif owed:
            store.flag_feature_attention(io_id, findings.review_headline(fo, report))
        else:
            store.clear_feature_attention(io_id)
        if fo.get("plan_wo_id"):
            # A separate kind for the OS deciding: `steward.touched` must count only the user
            kind = "findings_reviewed" if decided_by == "user" else "findings_decided"
            store.add_event(fo["plan_wo_id"], kind, {
                "improvement_order": io_id, "by": decided_by,
                "accepted": accepted_keys, "rejected": sorted(rejections),
                "created": [c["id"] for c in created],
                "errors": [e["error"] for e in errors],
            })
    finally:
        store.close()

    if stuck:
        # §5.2: a filing failure reaches the user's SINKS, not just a return value nobody reads
        central = CentralStore()
        try:
            central.add_inbox(
                project=name, level="warning",
                title=f"{io_id}: a finding you accepted could not be filed",
                body=reason, wo_id=fo.get("plan_wo_id") or None)
        finally:
            central.close()

    return {"project": name, "io_id": io_id, "status": status, "created": created,
            "learnings": learnings, "rejected": sorted(rejections), "errors": errors}


def _file_finding_orders(name: str, io_id: str, f: dict[str, Any],
                         decision: Any = None) -> tuple[list[dict[str, Any]], str]:
    """File the finding's proposed orders not filed yet: (created, error), stopping at the first failure"""
    from . import steward

    filed = f.setdefault("created_orders", [])
    done = {o.get("index") for o in filed}
    created: list[dict[str, Any]] = []
    error = ""
    for i, order in enumerate(f.get("proposed_orders") or []):
        if i in done:
            continue
        target = (order.get("project") or "").strip() or name
        order_type = order.get("type") or "work"
        # FIRST LINE names the improvement order: the worker sees this description and nothing else
        description = (
            f"Filed from improvement order {io_id} — run `jarvis io show {io_id}` "
            f"for the root cause this fixes.\n\n{order.get('description', '')}"
        )
        metadata: dict[str, Any] = {ORIGIN_IO_KEY: io_id}
        budget_usd = None
        if decision is not None:
            # Work nobody typed: on the evolution ledger, under the budget the steward set
            metadata = steward.stamped(metadata, decision)
            budget_usd = decision.budget_usd
        try:
            if order_type == "feature":
                row = create_feature_order(target, order.get("title", ""),
                                           description=description,
                                           metadata=metadata, budget_usd=budget_usd)
            else:
                row = create_work_order(target, order.get("title", ""),
                                        description=description, parent_id=None,
                                        metadata=metadata, budget_usd=budget_usd)
        except OpsError as e:
            error = str(e)
            break
        if decision is not None and not filed:
            # One decision, linked to the first order it led to
            steward.linked(decision, row["id"])
        filed.append({"index": i, "id": row["id"], "type": order_type,
                      "title": order.get("title", ""), "project": target})
        created.append({"id": row["id"], "type": order_type,
                        "title": order.get("title", "")})
    if error:
        f["filing_error"] = error
    else:
        f.pop("filing_error", None)
    return created, error


# -- Neo decides findings for the user


def findings_reviewed_by_neo() -> bool:
    """Is a findings report Neo's to decide?"""
    try:
        cfg = resolve_catalog().os.neo
    except (OpsError, CatalogError, OSError, ValueError) as e:
        # A FAILURE IS NEVER A VERDICT (kn-40db1828): nobody decides for the user on a guess
        log.warning("findings review: catalog unreadable (%s); the user decides", e)
        return False
    return bool(cfg.enabled and cfg.review_findings)


def _ask_findings_review(name: str, fo: dict[str, Any], report: dict[str, Any],
                         eligible: Callable[[dict[str, Any]], bool] | None = None
                         ) -> list[str]:
    """One atomic Neo question per eligible finding, its id kept on it; the keys it could not ask"""
    from . import findings
    from .neo_store import NeoStore, QuestionTooLargeError

    eligible = eligible or findings.never_offered
    # Hung off the analyst, as a plan question hangs off its planner; the io itself when there is none
    asker = fo.get("plan_wo_id") or fo["id"]
    context = findings.build_finding_context(fo, report)
    unasked: list[str] = []
    neo = NeoStore()
    try:
        for f in report["findings"]:
            if not eligible(f):
                continue
            # A re-ask starts clean: the old ruling or refusal no longer stands
            for field in ("neo_question_id", "neo_verdict", "neo_unasked"):
                f.pop(field, None)
            try:
                q = neo.ask(name, asker, findings.build_finding_question(fo, f),
                            context=context, kind="findings")
            except QuestionTooLargeError as e:
                # Refused, never trimmed: this one stays the user's, and is never offered again
                log.warning("[%s] %s/%s: not asked of Neo: %s", name, fo["id"], f["key"], e)
                f["neo_unasked"] = str(e)[:200]
                unasked.append(f["key"])
                continue
            f["neo_question_id"] = q["id"]
    finally:
        neo.close()
    return unasked


def offer_unasked_findings(name: str, path: Path) -> list[str]:
    """Put each waiting report's never-asked findings to Neo, once; the io ids offered"""
    from . import findings
    from .neo_store import NeoStore

    if not findings_reviewed_by_neo():
        return []
    store = ProjectStore(path)
    try:
        waiting = store.list_feature_orders(("plan_review",), kind="improvement")
    finally:
        store.close()
    offered: list[str] = []
    for fo in waiting:
        report = db.from_json(fo.get("plan"), None)
        if not isinstance(report, dict) or not any(
                isinstance(f, dict) and findings.never_offered(f)
                for f in report.get("findings") or []):
            continue
        if _offer_to_neo(name, path, fo, report, findings.never_offered, by="daemon"):
            offered.append(fo["id"])
    return offered


def _offer_to_neo(name: str, path: Path, fo: dict[str, Any], report: dict[str, Any],
                  eligible: Callable[[dict[str, Any]], bool], *, by: str
                  ) -> dict[str, list[str]] | None:
    """Ask Neo about one report's eligible findings and store the outcome; None when the io moved on"""
    from . import findings
    from .neo_store import NeoStore

    _ask_findings_review(name, fo, report, eligible)
    asked = {f["key"]: f for f in report["findings"] if isinstance(f, dict)}
    moot: list[int] = []
    out: dict[str, list[str]] | None = None
    store = ProjectStore(path)
    try:
        # Re-read: the user may have decided a finding while Neo was being asked
        fo = store.get_feature_order(fo["id"])
        current = db.from_json(fo.get("plan"), None)
        if fo["status"] != "plan_review" or not isinstance(current, dict):
            moot = [f["neo_question_id"] for f in asked.values() if f.get("neo_question_id")]
        else:
            out = {"asked": [], "not_asked": []}
            for f in current.get("findings") or []:
                src = asked.get(f.get("key")) if isinstance(f, dict) else None
                if src is None:
                    continue
                if eligible(f):
                    for field in ("neo_question_id", "neo_verdict", "neo_unasked"):
                        if src.get(field):
                            f[field] = src[field]
                        else:
                            f.pop(field, None)
                    if f.get("neo_question_id"):
                        out["asked"].append(f["key"])
                    elif f.get("neo_unasked"):
                        out["not_asked"].append(f["key"])
                elif src.get("neo_question_id") and \
                        src["neo_question_id"] != f.get("neo_question_id"):
                    moot.append(src["neo_question_id"])
            store.update_feature_order(fo["id"], plan=db.to_json(current))
            owed = [f for f in current.get("findings") or []
                    if isinstance(f, dict)
                    and (f.get("status") or "pending") == "pending"
                    and not findings.with_neo(f)]
            # The transition, never re-derived on a tick (kn-089de524)
            if owed:
                store.flag_feature_attention(fo["id"], findings.review_headline(fo, current))
            else:
                store.clear_feature_attention(fo["id"])
            if fo.get("plan_wo_id"):
                store.add_event(fo["plan_wo_id"], "findings_offered_to_neo", {
                    "improvement_order": fo["id"], "by": by,
                    "neo_questions": sum(1 for f in current.get("findings") or []
                                         if isinstance(f, dict) and findings.with_neo(f)),
                })
    finally:
        store.close()
    if moot:
        neo = NeoStore()
        try:
            for qid in moot:
                neo.supersede(qid, f"SUPERSEDED — {fo['id']} was decided while Neo was asked",
                              "the user decided this finding first")
        finally:
            neo.close()
    return out


def ask_neo_about_findings(io_id: str, project_name: str | None = None) -> dict[str, Any]:
    """`jarvis io ask-neo`: put a report's findings back to Neo, by hand"""
    from . import findings

    name, path, fo = find_feature_order(io_id, project_name)
    _require_kind(fo, "improvement", "jarvis fo show")
    if fo["status"] != "plan_review":
        raise OpsError(f"{io_id} is {fo['status']}: only a report waiting for decisions "
                       f"(`plan_review`) has findings to ask Neo about")
    if not findings_reviewed_by_neo():
        raise OpsError("Neo does not decide findings: `os.neo.enabled` or "
                       "`os.neo.review_findings` is off in the catalog")
    report = db.from_json(fo.get("plan"), None)
    if not isinstance(report, dict) or not any(
            isinstance(f, dict) and findings.reaskable(f)
            for f in report.get("findings") or []):
        return {"project": name, "io_id": io_id, "asked": [], "not_asked": [],
                "note": "nothing to ask: every finding is decided, already with Neo, or "
                        "accepted by Neo and waiting only on your yes to its spend"}
    out = _offer_to_neo(name, path, fo, report, findings.reaskable, by="user")
    if out is None:
        raise OpsError(f"{io_id} was decided while Neo was being asked; nothing changed")
    return {"project": name, "io_id": io_id, **out,
            "note": (f"{len(out['asked'])} finding(s) with Neo; its rulings arrive on the "
                     f"next Neo drain — `jarvis io show {io_id}`")
                    + (f". Too large to ask, still yours: {', '.join(out['not_asked'])}"
                       if out["not_asked"] else "")}


def _supersede_finding_questions(found: Sequence[Any], answer: str, why: str) -> None:
    """Close the Neo questions still open about these findings"""
    from . import findings
    from .neo_store import NeoStore

    open_ids = [f["neo_question_id"] for f in found
                if isinstance(f, dict) and findings.with_neo(f)]
    if not open_ids:
        return
    neo = NeoStore()
    try:
        for qid in open_ids:
            neo.supersede(qid, answer, why)
    finally:
        neo.close()


def _finding_for_question(q: Mapping[str, Any]
                          ) -> tuple[str, Path, dict[str, Any], dict[str, Any],
                                     dict[str, Any]] | None:
    """(project, path, io, report, finding) a findings question is about, or None"""
    path = registered_project_paths().get(str(q.get("project") or ""))
    if path is None:
        return None
    store = ProjectStore(path)
    try:
        fo = store.feature_order_for_planner(str(q.get("wo_id") or ""))
    finally:
        store.close()
    if not fo or fo.get("kind") != "improvement":
        return None
    report = db.from_json(fo.get("plan"), None)
    if not isinstance(report, dict):
        return None
    finding = next((f for f in report.get("findings") or []
                    if isinstance(f, dict) and f.get("neo_question_id") == q["id"]), None)
    if finding is None:
        return None
    return str(q["project"]), path, fo, report, finding


def _store_report(path: Path, io_id: str, report: dict[str, Any]) -> None:
    store = ProjectStore(path)
    try:
        store.update_feature_order(io_id, plan=db.to_json(report))
    finally:
        store.close()


def _finding_to_user(name: str, path: Path, fo: dict[str, Any], report: dict[str, Any],
                     finding: dict[str, Any], why: str) -> dict[str, Any]:
    """The OS could not decide this finding: it is the user's, with the reason attached"""
    from . import findings

    io_id, key = fo["id"], finding["key"]
    store = ProjectStore(path)
    try:
        store.update_feature_order(io_id, plan=db.to_json(report))
        # Raised at this transition only, never re-derived on a tick (kn-089de524)
        store.flag_feature_attention(
            io_id, f"{findings.review_headline(fo, report)} ({key}: {why})"[:400])
    finally:
        store.close()
    central = CentralStore()
    try:
        central.add_inbox(
            project=name, level="warning",
            title=f"{io_id}: finding {key!r} needs your decision",
            body=f"{fo['title']}\n{why}\n\n"
                 f"Read it with: jarvis io show {io_id} --finding {key}\n"
                 f"Then: jarvis io review {io_id} --accept {key}\n"
                 f"  or: jarvis io review {io_id} --reject {key} --feedback \"...\"",
            wo_id=fo.get("plan_wo_id") or None)
    finally:
        central.close()
    return {"project": name, "io_id": io_id, "key": key, "status": "plan_review",
            "decided": False, "reason": why}


def apply_findings_verdict(q: Mapping[str, Any], verdict: Mapping[str, Any]
                           ) -> dict[str, Any]:
    """Neo ruled on one finding: reject it, hand it to the user, or let the steward decide its spend"""
    from . import steward

    located = _finding_for_question(q)
    if located is None:
        return {"decided": False, "reason": f"question {q.get('id')} decides no finding"}
    name, path, fo, report, f = located
    io_id, key = fo["id"], f["key"]
    if fo["status"] != "plan_review" or (f.get("status") or "pending") != "pending" \
            or f.get("neo_verdict"):
        # The user got there first through `jarvis io review`
        return {"decided": False, "reason": f"{io_id}/{key} is already decided"}

    reason = " ".join(str(verdict.get("reason") or "").split())[:300]
    accepted = bool(verdict.get("approve")) and not verdict.get("escalate")
    severity = steward.normalise(str(verdict.get("severity") or ""))
    f["neo_verdict"] = {
        "question_id": q["id"], "escalate": bool(verdict.get("escalate")),
        "verdict": "" if verdict.get("escalate") else ("accept" if accepted else "reject"),
        "severity": severity, "reason": reason,
    }
    if verdict.get("escalate"):
        return _finding_to_user(name, path, fo, report, f,
                                f"Neo escalated it: {reason or 'no reason given'}")
    if not accepted:
        _store_report(path, io_id, report)
        return review_findings(io_id, reject={key: f"Neo: {reason or 'rejected'}"},
                               decided_by="neo", project_name=name)
    if not f.get("proposed_orders"):
        # A diagnosis with no work to file spends nothing, so nothing is the steward's to decide
        _store_report(path, io_id, report)
        return review_findings(io_id, accept=[key], feedback=f"Neo: {reason}",
                               decided_by="neo", project_name=name)

    try:
        catalog = resolve_catalog()
    except (OpsError, CatalogError, OSError, ValueError) as e:
        # A FAILURE IS NEVER A VERDICT: work nobody typed needs a yes
        return _finding_to_user(name, path, fo, report, f,
                                f"Neo accepts it, but the steward could not read the "
                                f"catalog to decide its spend ({str(e)[:120]})")
    store = ProjectStore(path)
    try:
        decision = steward.gate(
            steward.Candidate(
                "io_finding", severity, name,
                f"{io_id}/{key}: {' '.join(str(f.get('root_cause') or '').split())}"[:300],
                evidence=tuple(str(e.get("source") or "") for e in f.get("evidence") or []),
                parent_wo_id=str(fo.get("plan_wo_id") or ""),
                # Distinct records the improvement order was opened on: its recurrence
                refs=len(evidence_refs(fo)),
                # Everything `reoffer_io_finding` needs to file it later
                payload={"io_id": io_id, "key": key}),
            catalog=catalog, store=store)
    finally:
        store.close()
    f["steward"] = {"decision_id": decision.decision_id, "outcome": decision.outcome,
                    "rule": decision.rule, "reason": decision.reason,
                    "severity": decision.severity, "mode": decision.mode}
    _store_report(path, io_id, report)
    feedback = f"Neo: {reason}"
    if decision.allowed:
        return review_findings(io_id, accept=[key], feedback=feedback, decided_by="neo",
                               project_name=name, steward_decision=decision)
    self_dispatch = bool(catalog.os.autonomy.self_dispatch)
    if decision.mode == "enforce" and self_dispatch:
        if decision.outcome == "drop":
            return review_findings(
                io_id, reject={key: f"the steward drops it: {decision.reason}"},
                decided_by="steward", project_name=name)
        if decision.outcome == "queue":
            # The diagnosis stands now; its orders wait for room in the steward's queue
            return review_findings(io_id, accept=[key], feedback=feedback,
                                   decided_by="neo", project_name=name,
                                   steward_decision=decision, file_orders=False)
    why = (IO_SELF_DISPATCH_OFF if not self_dispatch
           else f"steward decision #{decision.decision_id}: {decision.reason}")
    return _finding_to_user(name, path, fo, report, f,
                            f"Neo accepts it ({severity}), and filing its orders needs "
                            f"your yes — {why}")


def findings_question_unanswered(q: Mapping[str, Any], detail: str) -> dict[str, Any]:
    """Neo could not be asked or reached: NOBODY decided, so the finding is the user's"""
    return apply_findings_verdict(q, {
        "escalate": True, "severity": "",
        "reason": f"Neo could not decide it — {detail[:200]}"})


def reoffer_io_finding(row: Mapping[str, Any], candidate: Any, decision: Any) -> str:
    """File the orders of a Neo-accepted finding the steward now has room for (`steward.drain`)"""
    held = candidate.payload or {}
    io_id, key = str(held.get("io_id") or ""), str(held.get("key") or "")
    name, path, fo = find_feature_order(io_id, candidate.project)
    report = db.from_json(fo.get("plan"), None) or {}
    f = next((x for x in report.get("findings") or []
              if isinstance(x, dict) and x.get("key") == key), None)
    if f is None or f.get("status") != "accepted" or not f.get("orders_held"):
        return ""  # decided or filed some other way meanwhile: nothing left to file
    created, error = _file_finding_orders(name, io_id, f, decision)
    if not error:
        f.pop("orders_held", None)
    store = ProjectStore(path)
    try:
        store.update_feature_order(io_id, plan=db.to_json(report))
        if fo.get("plan_wo_id"):
            store.add_event(fo["plan_wo_id"], "findings_decided", {
                "improvement_order": io_id, "by": "steward", "accepted": [],
                "rejected": [], "created": [c["id"] for c in created],
                "errors": [error] if error else []})
    finally:
        store.close()
    if error:
        raise OpsError(error)  # it stays queued, and is offered again next pass
    return created[0]["id"] if created else ""


# -- investigation orders

#: `feature_orders.metadata` key: the id this investigation is about
SUBJECT_KEY = "subject"

#: `feature_orders.metadata` key: `stuck.fingerprint` of the situation this investigation was opened... (docs/superpowers/specs/2026-09-30-an-order-that-stops-moving-gets-investigated.md)
STUCK_FINGERPRINT_KEY = "stuck_fingerprint"


def create_investigation_order(project_name: str | None, subject: str, why: str,
                               budget_usd: float | None = None,
                               origin: str = "jarvis",
                               fingerprint: str = "") -> dict[str, Any]:
    """Open an investigation into one stuck order"""
    paths = registered_project_paths()
    if project_name is not None and project_name not in paths:
        raise OpsError(f"project {project_name!r} not registered "
                       f"(known: {sorted(paths)}). Run `jarvis start` first.")
    subject = (subject or "").strip()
    if not subject:
        raise OpsError("an investigation needs a subject — the wo-/fo-/io- id that is "
                       "not progressing")
    if not (why or "").strip():
        raise OpsError(
            f"an investigation needs a `why`: the investigator's first reader is a fresh "
            f"session with no memory of the conversation that produced it. Say what you "
            f"saw — `jarvis investigate {subject} --why \"...\"`."
        )
    project_name, title, kind = _subject_identity(subject, project_name)
    if kind in ("investigation", "investigator"):
        raise OpsError(
            f"{subject} is an {kind} — an investigation never investigates the "
            f"diagnostician. Investigate the subject it was opened on instead."
        )
    live = live_investigation(project_name, subject)
    if live:
        raise OpsError(
            f"{live} is already investigating {subject} — `jarvis investigate show "
            f"{live}`. One live investigation per subject; open a new one once that has "
            f"settled."
        )
    store = ProjectStore(paths[project_name])
    try:
        return store.create_feature_order(
            title=f"investigate {subject}: {title}"[:200], description=why,
            origin=origin, kind="investigation",
            metadata={SUBJECT_KEY: subject,
                      **({STUCK_FINGERPRINT_KEY: fingerprint} if fingerprint else {})},
            # The family is this order plus its one investigator, so the family arithmetic is already correct
            budget_usd=(budget_usd if budget_usd is not None
                        else budget.investigation_default_for(
                            _spec_or_none(project_name))))
    finally:
        store.close()


def _subject_identity(subject: str,
                      project_name: str | None) -> tuple[str, str, str]:
    """The subject's project, title and kind, or an `OpsError` naming that it does not exist"""
    if is_feature_order_id(subject):
        name, _p, fo = find_feature_order(subject, project_name)
        return name, str(fo["title"]), str(fo.get("kind") or "feature")
    name, _p, wo = find_work_order(subject, project_name)
    return name, str(wo["title"]), str(wo.get("kind") or "worker")


def live_investigation(project_name: str, subject: str) -> str:
    """The id of the non-terminal investigation already on this subject, or `""`"""
    paths = registered_project_paths()
    store = ProjectStore(paths[project_name])
    try:
        for row in store.list_feature_orders(statuses=FO_OPEN_STATUSES,
                                             kind="investigation"):
            metadata = db.from_json(row.get("metadata"), {}) or {}
            if metadata.get(SUBJECT_KEY) == subject:
                return str(row["id"])
    finally:
        store.close()
    return ""


#: The name every caller before §6a used
_live_investigation = live_investigation


def last_stuck_investigation(project_name: str, subject: str) -> dict[str, Any] | None:
    """The newest investigation of ANY status on this subject that carries a fingerprint"""
    paths = registered_project_paths()
    store = ProjectStore(paths[project_name])
    try:
        for row in store.list_feature_orders(statuses=None, kind="investigation"):
            metadata = db.from_json(row.get("metadata"), {}) or {}
            if metadata.get(SUBJECT_KEY) == subject \
                    and metadata.get(STUCK_FINGERPRINT_KEY):
                return {**row, STUCK_FINGERPRINT_KEY: str(
                    metadata[STUCK_FINGERPRINT_KEY])}
    finally:
        store.close()
    return None


def manager_awaiting_feature(
    pstore: ProjectStore, wo: dict[str, Any], now: float | None = None
) -> bool:
    """Is this a feature manager whose feature can still wake it?"""
    if wo.get("kind") != "manager" or not wo.get("parent_id"):
        return False
    try:
        parent = pstore.get_feature_order(wo["parent_id"])
    except KeyError:
        return False  # deleted out from under the child: nothing left to wake it
    return parent["status"] not in FO_TERMINAL_STATUSES


def stuck_scan(pstore: ProjectStore, project_name: str, cfg: Any, now: float, *,
               window: bool = False,
               base_red: bool = False,
               red_episodes: Sequence[Any] = (),
               base_red_until: float = 0.0,
               stuck_only: bool = True) -> tuple[int, list[dict[str, Any]]]:
    """Every open order of one project, judged by `stuck.assess`. §7's ONE derivation (docs/superpowers/specs/2026-10-15-a-merge-held-by-a-red-base-is-not-stuck.md)"""
    from . import budget, bus, holds, stuck, worker_session
    from .central_store import base_red_hold_spans
    from .health import observer_kinds
    from .project_store import (FO_ID_PREFIXES, PLAN_ASSUMPTION_EVENT_KINDS,
                                UNGOVERNED_ORIGINS, VALIDATION_CI_CAUSE,
                                plan_hold_spans, precondition_hold_spans,
                                validation_ci_hold_spans, validation_hold)
    from .worker_session import PAUSE_USAGE_LIMIT

    thresholds = {s: cfg.threshold_seconds(s) for s in cfg.thresholds}
    scanned, out = 0, []
    for wo in pstore.list_work_orders(statuses=OPEN_STATUSES):
        if wo["origin"] in UNGOVERNED_ORIGINS:
            continue  # the user's own session — `_health_candidates`' rule
        # AN INVESTIGATION IS NEVER A SUBJECT
        if str(wo.get("parent_id") or "").startswith(
                f'{FO_ID_PREFIXES["investigation"]}-'):
            continue
        scanned += 1
        durations = state_durations(pstore, wo_id=wo["id"], now=now)
        if durations.current_status_since is None:
            continue  # no present-tense claim to judge — `state_durations`' own rule
        spans = holds.held(pstore, wo["id"], now=now)
        since = durations.current_status_since
        usage = [h for h in spans if h.cause == PAUSE_USAGE_LIMIT]
        # AN ORDER AT ITS BUDGET CAP IS NOT STUCK (§2 of docs/superpowers/specs/...
        budget_spans = ([(max(h.started, since), min(h.finish(now), now))
                         for h in spans if h.cause == holds.BUDGET]
                        if wo["status"] == budget.EXHAUSTED else [])
        # A ROUND WAITING FOR GITHUB'S CHECKS IS NOT STUCK (§1 and §3 of... (docs/superpowers/specs/2026-10-12-a-validation-round-waiting-for-ci-is-not-stuck.md)
        ci_held, ci_spans = False, []
        if wo["status"] == "validating":
            latest = pstore.latest_validation_round(wo_id=wo["id"])
            events = pstore.events_of_kind(wo["id"], "validation_failed")
            if latest:
                until, cause = validation_hold(events, int(latest["round"]))
                ci_held = cause == VALIDATION_CI_CAUSE and until > now
            ci_spans = validation_ci_hold_spans(events, since, now)
        # A MERGE THE OS IS ITSELF HOLDING ONTO A RED BASE IS NOT STUCK (§1 and §2 of... (docs/superpowers/specs/2026-10-15-a-merge-held-by-a-red-base-is-not-stuck.md)
        base_red_spans = (base_red_hold_spans(red_episodes, since, now, base_red_until)
                          if wo["status"] == "waiting_pr_merge" else [])
        # A MERGE THE OS IS ITSELF HOLDING FOR GITHUB'S RUNNING CHECKS IS NOT STUCK (§2 and §3 of...
        automerge_checks_held, automerge_spans, am_note = False, [], ""
        if wo["status"] == "waiting_pr_merge":
            am_note = invariants.automerge_checks_note(automerge_state(pstore, wo))
            automerge_checks_held = bool(am_note) and base_red_until >= now
            # `events_of_kind` takes ONE kind
            automerge_spans = automerge_checks_hold_spans(
                [e for k in AUTOMERGE_EVENTS
                 for e in pstore.events_of_kind(wo["id"], k)],
                since, now, base_red_until)
        # A CHILD HELD BY ITS FEATURE'S UNRATIFIED PLAN IS NOT STUCK (§2.3 and §2.4 of...
        plan_held, plan_spans = False, []
        if (wo["status"] == "pending" and wo.get("parent_id")
                and wo.get("kind") == "worker"):
            plan_held = bool(pstore.plan_hold(wo))
            try:
                planner_id = pstore.get_feature_order(
                    str(wo["parent_id"])).get("plan_wo_id")
            except KeyError:
                planner_id = None  # the feature was deleted out from under the child
            if planner_id:
                # `events_of_kind` takes ONE kind
                plan_spans = plan_hold_spans(
                    sorted((e for k in PLAN_ASSUMPTION_EVENT_KINDS
                            for e in pstore.events_of_kind(str(planner_id), k)),
                           key=lambda e: float(e["ts"])), since, now)
        # AN ORDER HELD BY AN OPEN PRECONDITION IS NOT STUCK (§2
        pre_held, pre_spans = False, []
        if wo["status"] == "pending":
            pre_held = bool(pstore.open_preconditions(wo["id"]))
            pre_spans = precondition_hold_spans(
                pstore.preconditions_of(wo["id"]), since, now)
        # The UNION and never the sum: a usage window can shut while CI runs (§4)
        discounted = holds.union_seconds(
            [(max(h.started, since), min(h.finish(now), now)) for h in usage]
            + ci_spans + base_red_spans + plan_spans + automerge_spans
            + budget_spans + pre_spans)
        # §2.2 of docs/superpowers/specs/2026-10-09-a-designed-wait-is-not-a-stall.md
        edges = (pstore.unfinished_dependencies(wo["id"])
                 if wo["status"] == "pending" else [])
        live_edges = bool(edges) and not any(
            invariants.dependency_is_dead(dep) for dep in edges)
        # IS THIS MANAGER WAITING CORRECTLY? (#1008)
        by_design_idle = bool(
            wo["status"] == "idle" and manager_awaiting_feature(pstore, wo)
            and (invariants.outstanding_feature_children(pstore, str(wo["parent_id"]))
                 or not bus.nothing_queued_to_manager(pstore, str(wo["parent_id"]),
                                                      str(wo["id"]))))
        # §6 of docs/superpowers/specs/2026-10-14-a-worker-turn-in-flight-is-not-stuck.md
        turn_in_flight = bool(wo["status"] == "running"
                              and worker_session.busy(pstore, wo["id"]))
        verdict = stuck.assess(
            str(wo["status"]), now - since, now - (durations.last_activity_ts or since),
            discounted, thresholds, cfg.fallback_minutes * 60.0,
            # CI and base-red cannot collide: `validating` and `waiting_pr_merge` (§1 of the 2026-10-15 spec)
            excluded_cause=(PAUSE_USAGE_LIMIT
                            if window or any(h.open for h in usage)
                            else VALIDATION_CI_CAUSE if ci_held
                            else stuck.BASE_RED_CAUSE
                            if base_red and wo["status"] == "waiting_pr_merge"
                            # AFTER the red base and BEFORE the dependency: both are `waiting_pr_merge`-only and CAN co-occur
                            else stuck.AUTOMERGE_CHECKS_CAUSE if automerge_checks_held
                            # DOCUMENTARY, not load-bearing (§3 of the 2026-10-20 spec): a collision is impossible by...
                            else stuck.BUDGET_CAP_CAUSE
                            if wo["status"] == budget.EXHAUSTED
                            # Above both `pending` holds, §3.3 (Neo 1982): `status_label` keeps the same ranking
                            else stuck.PRECONDITION_CAUSE if pre_held
                            else stuck.LIVE_DEPENDENCY if live_edges
                            # LAST in the chain, §2.4: a fleet fact outranks a fact about one feature's plan
                            else stuck.PLAN_ASSUMPTION_CAUSE if plan_held else ""),
            kind=str(wo.get("kind") or "worker"),
            exempt_kinds=tuple(cfg.exempt_kinds),
            by_design_idle=by_design_idle,
            turn_in_flight=turn_in_flight)
        if stuck_only and not verdict.stuck:
            continue
        blocker = true_blockers(pstore, wo, now=now)
        out.append({
            "project": project_name, "wo": wo, "verdict": verdict,
            # The outstanding-child note before the empty answer: one formatter shared with `status_label` The...
            "blocker": (blocker[0] if blocker
                        else invariants.outstanding_children_note(pstore, wo)
                        or invariants.precondition_note(
                            pstore.open_preconditions(wo["id"]) if pre_held else [])
                        or invariants.plan_hold_note(
                            pstore.plan_hold(wo) if plan_held else None)
                        # §5 of the 2026-10-20 spec, after the plan hold, which is `pending`-only and cannot co-occur with it
                        or am_note
                        or "nothing the record can name"),
            "status_label": invariants.status_label(pstore, wo),
            "since": since, "activity": durations.last_activity_ts or since,
            "discounted": discounted,
            "seconds_in_status": now - since,
            "seconds_since_activity": now - (durations.last_activity_ts or since),
            "events": pstore.count_events(wo["id"], exclude=observer_kinds()),
        })
    return scanned, out


def stuck_report(project_name: str | None = None,
                 now: float | None = None) -> list[dict[str, Any]]:
    """Every open order and how the sweep judges it. §7: ONE reader, two renderers"""
    from . import db, stuck

    now = db.now() if now is None else now
    catalog = resolve_catalog()
    paths = registered_project_paths()
    rows: list[dict[str, Any]] = []
    for spec in catalog.projects:
        if not spec.fleet_health.enabled or spec.name not in paths:
            continue
        if project_name and spec.name != project_name:
            continue
        # ONCE PER PROJECT, never once per candidate — §1 of the 2026-10-15 spec
        base_red, red_episodes, base_red_until = False, (), 0.0
        if spec.validation.enabled and spec.validation.auto_merge:
            fact = _stored_base_health(spec.name)
            base_red = _base_is_red(spec, now, fact)
            base_red_until = _base_red_until(fact, now)
            red_episodes = tuple(_stored_base_red_episodes(spec.name))
        pstore = ProjectStore(paths[spec.name])
        try:
            _scanned, judged = stuck_scan(
                pstore, spec.name, spec.fleet_health, now,
                window=bool(pstore.health_sweep_hold()), base_red=base_red,
                red_episodes=red_episodes, base_red_until=base_red_until,
                stuck_only=False)
        finally:
            pstore.close()
        for row in judged:
            wo, verdict = row["wo"], row["verdict"]
            rows.append({
                "id": wo["id"], "project": spec.name, "title": wo["title"],
                "status": wo["status"], "status_label": row["status_label"],
                "seconds_in_status": row["seconds_in_status"],
                "seconds_since_activity": row["seconds_since_activity"],
                "discounted_seconds": row["discounted"],
                "active_seconds": verdict.active_seconds,
                "threshold_seconds": verdict.threshold_seconds,
                "clock": verdict.clock, "stuck": verdict.stuck,
                "excluded": verdict.excluded, "reason": verdict.reason,
                "blocker": row["blocker"],
                "fingerprint": stuck.fingerprint(str(wo["status"]), row["since"],
                                                 row["blocker"], row["events"]),
                "investigation": _stuck_investigation(spec.name, wo["id"]),
            })
    rows.sort(key=lambda r: r["threshold_seconds"] - r["active_seconds"])
    return rows


def _stuck_investigation(project_name: str, wo_id: str) -> dict[str, Any] | None:
    """The live or last investigation of this subject, with its verdict when settled"""
    live = live_investigation(project_name, wo_id)
    last = last_stuck_investigation(project_name, wo_id)
    row = None
    if live:
        _n, _p, row = find_feature_order(live, project_name)
    elif last is not None:
        row = last
    if row is None:
        return None
    plan = db.from_json(row.get("plan"), {}) or {}
    return {"id": row["id"], "status": row["status"],
            "classification": str(plan.get("classification") or "")}


def list_investigation_orders(project_name: str | None = None,
                              include_settled: bool = False) -> list[dict[str, Any]]:
    """`jarvis investigate list`"""
    return list_feature_orders(project_name, include_settled=include_settled,
                              kind="investigation")


def show_investigation_order(inv_id: str, project_name: str | None = None
                             ) -> dict[str, Any]:
    """`jarvis investigate show` — THE CLASSIFICATION AND THE SUBJECT FIRST"""
    name, path, fo = find_feature_order(inv_id, project_name)
    _require_kind(fo, "investigation", "jarvis fo show")
    # `plan` is NULL until the verdict is submitted, which is the NORMAL state here
    verdict = db.from_json(fo.get("plan"), None) or {}
    if not isinstance(verdict, dict):
        verdict = {}
    metadata = db.from_json(fo.get("metadata"), {}) or {}
    store = ProjectStore(path)
    try:
        investigator = None
        calls: list[dict[str, Any]] = []
        if fo.get("plan_wo_id"):
            try:
                row = store.get_work_order(fo["plan_wo_id"])
                investigator = {k: row[k] for k in ("id", "title", "status",
                                                    "result_summary")}
                # THE CALLS THE DIAGNOSIS RESTS ON: its audit trail, not a review item
                calls = [{"n": a["n"], "content": a["content"]}
                         for a in store.all_assumptions(row["id"])]
            except KeyError:
                investigator = None  # deleted out from under it; the link was released
        alarms = store.alarms_for_feature(inv_id)
    finally:
        store.close()
    return {
        "project": name, **fo,
        "subject": metadata.get(SUBJECT_KEY) or "",
        "why": fo["description"],
        "verdict": verdict,
        "classification": verdict.get("classification") or "",
        "filed": verdict.get("filed") or None,
        "filing_error": verdict.get("filing_error") or "",
        "investigator": investigator,
        "assumptions": calls,
        "status_label": feature_status_label("investigation", fo["status"]),
        "alarms": alarms,
    }


def cancel_investigation_order(inv_id: str, project_name: str | None = None
                               ) -> dict[str, Any]:
    """`jarvis investigate cancel`"""
    _, _, fo = find_feature_order(inv_id, project_name)
    _require_kind(fo, "investigation", "jarvis fo cancel")
    return cancel_feature_order(inv_id, project_name)


def investigation_order_budget(inv_id: str, project_name: str | None = None
                               ) -> dict[str, Any]:
    """`jarvis investigate budget`, read-only — THE SECOND STEP OF THE TOP-UP (docs/superpowers/specs/2026-10-01-a-family-capped-raise-must-say-so.md)"""
    _, _, fo = find_feature_order(inv_id, project_name)
    _require_kind(fo, "investigation", "jarvis fo budget")
    return feature_order_budget(inv_id, project_name)


def set_investigation_budget(inv_id: str, amount: float | None,
                             project_name: str | None = None) -> dict[str, Any]:
    """`jarvis investigate budget <amount>`"""
    _, _, fo = find_feature_order(inv_id, project_name)
    _require_kind(fo, "investigation", "jarvis fo budget")
    return set_feature_budget(inv_id, amount, project_name)


def submit_verdict(inv_id: str, doc: Any,
                   project_name: str | None = None) -> dict[str, Any]:
    """(Investigators) hand back the verdict (#792)"""
    from . import bugreport, verdicts

    name, path, fo = find_feature_order(inv_id, project_name)
    _require_kind(fo, "investigation", "jarvis fo plan")
    if fo["status"] != "planning":
        raise OpsError(
            f"{inv_id} is {fo['status']}, so it is not waiting for a verdict. There is "
            f"no review round for an investigation — the verdict settles the order, and "
            f"a second one is refused here rather than by a special case."
        )
    metadata = db.from_json(fo.get("metadata"), {}) or {}
    subject = str(metadata.get(SUBJECT_KEY) or "")
    try:
        verdict = verdicts.parse_verdict(doc, subject=subject)
    except verdicts.VerdictError as e:
        raise OpsError(
            f"the verdict was not accepted, and nothing was stored. Fix all of these and "
            f"resubmit:\n  - " + "\n  - ".join(e.problems)
        ) from e

    verdict["classified_by"] = "investigator"
    verdict["filed"] = None
    filing_error = ""
    if verdict["classification"] == "GAP":
        try:
            duplicate = _verdict_duplicate(name, subject, verdict, fo)
        except Exception as e:  # noqa: BLE001 — an unreadable tracker is the §2.5 case
            filing_error = f"the duplicate check could not read the tracker: {e}"
            duplicate = ""
        if not filing_error and duplicate:
            # RECORDED AS `ops`' DECISION, and the submitted classification is kept: the record must never read...
            verdict["submitted_classification"] = "GAP"
            verdict["classification"] = "ALREADY_TRACKED"
            verdict["classified_by"] = "ops"
            verdict["duplicate_of"] = duplicate
        elif not filing_error:
            try:
                filed = bugreport.report_bug(
                    title=verdict["proposed_fix"]["title"],
                    description=verdict["proposed_fix"]["description"],
                    expected=verdict["proposed_fix"]["expected"],
                    actual=verdict["proposed_fix"]["actual"],
                    priority=verdict["proposed_fix"]["priority"],
                    project=name, wo_id=fo.get("plan_wo_id") or "",
                    expedite=False)  # file only: the user picks it up (os.autonomy)
            except Exception as e:  # noqa: BLE001 — same case, one step later
                filing_error = f"the bug could not be filed: {e}"
            else:
                verdict["filed"] = {
                    "issue_url": filed.get("url"),
                    "wo_id": (filed.get("pickup") or {}).get("wo_id"),
                    "expedited": False,
                }

    if filing_error:
        return _verdict_filing_failed(name, path, fo, inv_id, verdict, filing_error)

    classification = verdict["classification"]
    store = ProjectStore(path)
    try:
        store.update_feature_order(inv_id, plan=db.to_json(verdict))
        store.set_feature_status(inv_id, "completed")
        # THE ONE CLASSIFICATION THAT SPENDS ATTENTION
        if classification == "WAITING_ON_USER":
            store.flag_feature_attention(inv_id,
                                         verdicts.settle_headline(inv_id, verdict))
        else:
            store.clear_feature_attention(inv_id)
        # …AND ON THE SUBJECT'S OWN TIMELINE (§7): the line above is the INVESTIGATOR's work order
        if subject and not is_feature_order_id(subject):
            try:
                store.add_event(subject, "investigation_verdict", {
                    "investigation": inv_id, "classification": classification,
                    "filed": (verdict.get("filed") or {}).get("issue_url"),
                })
            except (KeyError, sqlite3.IntegrityError):
                pass  # not this project's work order any more
        if fo.get("plan_wo_id"):
            store.add_event(fo["plan_wo_id"], "verdict_submitted", {
                "investigation": inv_id, "classification": classification,
                "classified_by": verdict["classified_by"],
                "filed": (verdict.get("filed") or {}).get("issue_url"),
            })
    finally:
        store.close()

    out: dict[str, Any] = {
        "project": name, "inv_id": inv_id, "status": "completed",
        "classification": classification, "filed": verdict.get("filed"),
        "classified_by": verdict["classified_by"],
        "note": "the investigation is settled — end your turn.",
    }
    if fo.get("plan_wo_id"):
        # AFTER THE VERDICT IS DURABLE: a failure here is not a rejection
        try:
            settled = _settle_submitter(path, fo["plan_wo_id"],
                                        f"submitted a {classification} verdict for "
                                        f"{inv_id}", "verdict_submitted_settled",
                                        {"investigation": inv_id,
                                         "classification": classification})
        except (OpsError, sqlite3.Error) as e:
            out["warning"] = submitter_unsettled("verdict", inv_id, fo["plan_wo_id"], e)
        else:
            if settled is not None:
                out["investigator"] = settled
    return out


def _settle_submitter(path: Path, wo_id: str, summary: str, event: str,
                      payload: dict[str, Any]) -> dict[str, Any] | None:
    """Settle the work order whose submission was just stored, BY ITS KIND'S ROUTE"""
    store = ProjectStore(path)
    try:
        try:
            wo = store.get_work_order(wo_id)
        except KeyError:
            return None  # deleted; the link was released
        route = kind_contract(wo).route
        if route == SETTLE_IN_SESSION:
            store.update_work_order(wo_id, result_summary=summary)
            settle_in_session(store, wo, event, payload=payload)
            return {"wo_id": wo_id, "status": "completed", "session_stopped": False}
    finally:
        store.close()
    if route == SETTLE_BY_FINISH:
        return finish(wo_id, summary)
    raise OpsError(f"{wo_id} is a {wo.get('kind') or 'worker'}, whose kind settles by "
                   f"{route!r} and never by a submission")


def submitter_unsettled(what: str, order_id: str, wo_id: str, error: Exception) -> str:
    """The submission IS stored; only settling its work order failed (io-b521d17b)"""
    return (f"your {what} for {order_id} IS stored and {order_id} has taken it — do "
            f"NOT resubmit it. Only settling your work order {wo_id} failed, and that "
            f"is the OS's to clear, not yours: {error}")


def _verdict_duplicate(project: str, subject: str, verdict: dict[str, Any],
                       fo: dict[str, Any]) -> str:
    """What already covers this GAP — a tracker issue url or a live order id — or `""`"""
    from . import bugreport, issues, search as search_mod

    title = (verdict.get("proposed_fix") or {}).get("title") or ""
    repo = bugreport.bug_repo()
    if title:
        for row in issues.issues_mentioning(repo, title):
            return str(row["url"])
    ours = {subject, fo["id"], fo.get("plan_wo_id") or ""}
    for hit in search_mod.search(subject, project=project,
                                 kinds=("work_order", "feature_order")):
        if hit["id"] in ours or hit.get("status") in TERMINAL_STATUSES:
            continue
        return str(hit["id"])
    return ""


def _verdict_filing_failed(name: str, path: Path, fo: dict[str, Any], inv_id: str,
                           verdict: dict[str, Any], error: str) -> dict[str, Any]:
    """§2.5's one attention case that is not a classification: `gh` was unreachable"""
    verdict["filing_error"] = error
    reason = (f"{inv_id}: the verdict could not be filed — {error} — retry with "
              f"`jarvis investigate verdict {inv_id} --from-file verdict.json`")
    store = ProjectStore(path)
    try:
        store.update_feature_order(inv_id, plan=db.to_json(verdict))
        store.flag_feature_attention(inv_id, reason)
    finally:
        store.close()
    central = CentralStore()
    try:
        central.add_inbox(
            project=name, level="warning",
            title=f"{inv_id}: an investigation's verdict could not be filed",
            body=reason, wo_id=fo.get("plan_wo_id") or None)
    finally:
        central.close()
    return {"project": name, "inv_id": inv_id, "status": fo["status"],
            "classification": verdict["classification"], "filed": None,
            "filing_error": error, "note": reason}


def show_feature_order(fo_id: str, project_name: str | None = None) -> dict[str, Any]:
    """The feature order, its plan and its children — the tree, in one call"""
    from . import plans

    name, path, fo = find_feature_order(fo_id, project_name)
    # The verb for the kind the row ACTUALLY is: this is the surface an id of another kind reaches by... (#997)
    _require_kind(fo, "feature", show_verb(fo.get("kind")))
    store = ProjectStore(path)
    try:
        plan = db.from_json(fo.get("plan"), None)
        children = [
            {**{k: c[k] for k in ("id", "title", "status", "needs_attention",
                                  "attention_reason", "pr_url", "superseded")},
             "depends_on": store.dependencies(c),
             "status_label": invariants.status_label(store, c)}
            for c in store.feature_children(fo_id, kinds=WORKER_CHILDREN)
        ]
        planner = None
        if fo.get("plan_wo_id"):
            try:
                p = store.get_work_order(fo["plan_wo_id"])
                planner = {k: p[k] for k in ("id", "title", "status", "result_summary")}
            except KeyError:
                planner = None  # deleted out from under it; the link was released
        # The other session that belongs to this feature without being a piece of its work
        mgr = store.manager_work_order(fo_id)
        manager = ({k: mgr[k] for k in ("id", "title", "status", "result_summary")}
                   if mgr else None)
        return {
            "project": name, **fo,
            "plan": plan,
            "plan_text": "\n".join(plans.render_plan(plan)) if plan else "",
            "planner": planner,
            "manager": manager,
            # The feature's OWN rounds, never its children's: a child's review is on the child's page
            "validation_rounds": validation_rounds(store, fo_id=fo_id),
            # The feature's OWN follow-ups — the ones its plan review raised
            "issues": issue_index(store, fo_id),
            # Every finding ABOUT this feature
            "alarms": store.alarms_for_feature(fo_id),
            # How long it has been where it is, on the same always-present rule
            "time_in_state": state_durations(store, fo_id=fo_id).as_dict(),
            "children": children,
            "progress": feature_progress(store, fo),
            # Only meaningful next to `max_parallel`
            "active_children": store.count_active_children(fo_id),
        }
    finally:
        store.close()


def _planner_work_order(path: Path, fo: dict[str, Any]) -> dict[str, Any] | None:
    """The feature's planner row, for `landing.committed_text`'s first rung"""
    if not fo.get("plan_wo_id"):
        return None
    store = ProjectStore(path)
    try:
        return store.get_work_order(fo["plan_wo_id"])
    finally:
        store.close()


def _spec_branch(path: Path, planner_wo: dict[str, Any] | None) -> str:
    """Which branch the refusal below tells the planner to commit on"""
    from . import evidence, landing

    worktree = landing.worktree_of(path, planner_wo) if planner_wo else None
    branch = landing.authored(worktree).branch if worktree else ""
    return branch or evidence.base_ref(path) or "the default branch"


def _record_spec_pull_request(path: Path, fo_id: str,
                              planner_wo: dict[str, Any] | None,
                              design_doc: str) -> None:
    """The planner's spec pull request, onto its own record — or refuse the submission (docs/superpowers/specs/2026-09-27-a-planner-submits-behind-its-spec-pull-request.md)"""
    from . import github

    if not planner_wo:
        return
    store = ProjectStore(path)
    try:
        recorded = store.get_work_order(planner_wo["id"])
        if recorded is None or recorded.get("pr_url"):
            return
        work = authorship(store, planner_wo)
        if not work.produced:
            return
        branch = work.branch or _spec_branch(path, planner_wo)
        try:
            pr_url = github.open_pull_request_for_branch(branch, path)
        except github.GhUnavailable as e:
            raise OpsError(
                f"{fo_id}'s plan was not stored: the `gh` CLI is not installed where the "
                f"OS can reach it, so the planner's pull request cannot be confirmed. "
                f"Resubmit once `gh` works.\n{e}"
            ) from e
        except github.GitHubError as e:
            raise OpsError(
                f"{fo_id}'s plan was not stored: the pull request on `{branch}` could "
                f"not be confirmed — {e.reason}. Nothing is being abandoned; resubmit "
                f"once `gh` works."
            ) from e
        if not pr_url:
            raise OpsError(
                f"{fo_id}'s planner has committed `{design_doc}` on `{branch}`, and "
                f"there is no OPEN pull request on that branch — so the spec would stay "
                f"on the branch and nothing would ever land it. Push the branch and open "
                f"a pull request, then run `jarvis fo plan` again: the plan is not stored "
                f"until this passes.\n"
                f"  git push -u origin {branch} && gh pr create --fill\n"
                f"If the spec has already merged, reset the branch onto `origin/main` — "
                f"confirm the files diff empty against it first — rather than opening a "
                f"second pull request."
            )
        store.update_work_order(planner_wo["id"], pr_url=pr_url)
        store.add_event(planner_wo["id"], "pr_url_recorded",
                        {"pr_url": pr_url, "feature_order": fo_id,
                         "source": "plan_submit"})
    finally:
        store.close()


def _ask_plan_review(name: str, fo: dict[str, Any], plan: dict[str, Any],
                     planner_id: str, source: str, why: str) -> dict[str, Any]:
    """Ask for a review of this plan, closing whichever review it replaces"""
    from . import plans
    from .neo_store import NeoStore

    neo = NeoStore()
    try:
        q = neo.ask(name, planner_id, plans.build_plan_question(fo, plan),
                    context=plans.build_plan_context(plan, source), kind="plan")
        if fo.get("plan_question_id"):
            neo.supersede(
                fo["plan_question_id"],
                f"SUPERSEDED by question {q['id']}",
                f"{why}; question {q['id']} reviews the version that replaced the one "
                f"this asks about",
            )
    finally:
        neo.close()
    return q


def submit_plan(fo_id: str, doc: Any,
                project_name: str | None = None) -> dict[str, Any]:
    """(Planners) hand back the decomposition"""
    from . import landing, plans

    name, path, fo = find_feature_order(fo_id, project_name)
    _require_kind(fo, "feature", "jarvis io report")
    if fo["status"] not in ("planning", "plan_review"):
        raise OpsError(
            f"{fo_id} is {fo['status']}, so it is not waiting for a plan "
            f"(a plan can be submitted while it is `planning`, or resubmitted while it "
            f"is `plan_review`)"
        )
    try:
        plan = plans.parse_plan(doc)
    except plans.PlanError as e:
        raise OpsError(
            f"the plan was not accepted, and nothing was created. Fix all of these and "
            f"resubmit:\n  - " + "\n  - ".join(e.problems)
        ) from e

    # The spec is snapshotted NOW, from the planner's own tree
    planner_wo = _planner_work_order(path, fo)
    found = landing.committed_text(path, planner_wo, plan["design_doc"])
    if found is None:
        raise OpsError(
            f"the plan names design_doc {plan['design_doc']!r} but no COMMITTED copy "
            f"exists on {_spec_branch(path, planner_wo)} — the reviewer is sent the "
            f"committed text, never your working tree. Commit it and resubmit:\n"
            f"  git add {plan['design_doc']} && git commit -m \"spec: …\""
        )
    # THE REVISION, PERSISTED IN THE SAME STATEMENT AS THE TEXT, so the two can never disagree (docs/superpowers/specs/2026-09-28-a-feature-spec-you-can-open.md)
    plan["design_doc_content"], plan["design_doc_source"] = found
    source = plan["design_doc_source"]
    spec_problems = plans.spec_problems(plan, plan["design_doc_content"])
    if spec_problems:
        raise OpsError(
            f"the plan was not accepted, and nothing was created. Fix all of these and "
            f"resubmit:\n  - " + "\n  - ".join(spec_problems)
        )

    # §3: after the committed-copy check
    _record_spec_pull_request(path, fo_id, planner_wo, plan["design_doc"])

    # The planner is who Neo's question hangs off: it is a real work order
    planner_id = fo.get("plan_wo_id") or fo_id
    q = _ask_plan_review(name, fo, plan, planner_id, source,
                         why="the plan was revised and resubmitted")

    store = ProjectStore(path)
    try:
        store.update_feature_order(fo_id, plan=db.to_json(plan),
                                   plan_question_id=q["id"])
        store.set_feature_status(fo_id, "plan_review")
        store.clear_feature_attention(fo_id)
        if fo.get("plan_wo_id"):
            store.add_event(fo["plan_wo_id"], "plan_submitted", {
                "feature_order": fo_id, "children": len(plan["children"]),
                "neo_question_id": q["id"],
            })
    finally:
        store.close()

    out = {"project": name, "fo_id": fo_id, "status": "plan_review",
           "children": len(plan["children"]), "neo_question_id": q["id"],
           "note": "queued for review — end your turn. If it is sent back, the reason "
                   "arrives as your next user turn and you revise from this session."}
    if fo.get("plan_wo_id"):
        # The planner has no more to say until the review lands
        summary = f"submitted a plan for {fo_id}: {len(plan['children'])} work orders"
        try:
            out["planner"] = _settle_submitter(path, fo["plan_wo_id"], summary,
                                               "plan_submitted_settled",
                                               {"feature_order": fo_id})
        except OpsError as e:
            # docs/superpowers/specs/2026-09-27-a-planner-submits-behind-its-spec-pull-request.md
            out["warning"] = _planner_unsettled(path, fo_id, fo["plan_wo_id"],
                                                summary, e)
    return out


def _planner_unsettled(path: Path, fo_id: str, planner_id: str, summary: str,
                       refusal: OpsError) -> str:
    """The plan IS stored; only the planner could not settle (#822)"""
    store = ProjectStore(path)
    try:
        ids = [str(a["id"]) for a in store.open_approvals(planner_id)]
    finally:
        store.close()
    blocker = (f"gate request{'s' if len(ids) > 1 else ''} {', '.join(ids)} still open"
               if ids else "it could not be settled")
    return (
        f"{fo_id}'s plan IS stored and queued for review — nothing here needs "
        f"resubmitting, and resubmitting would ask Neo a second time about the same "
        f"plan. Only its planner {planner_id} is unsettled: {blocker}. Clear that, then "
        f"settle the planner:\n"
        f"    jarvis wo finish {planner_id} --summary \"{summary}\"\n\n{refusal}"
    )


def refresh_plan_spec(fo_id: str,
                      project_name: str | None = None) -> dict[str, Any]:
    """Keep the spec under review equal to the spec as committed. (B) of issue #746"""
    from . import landing, plans
    from .neo_store import NeoStore

    name, path, fo = find_feature_order(fo_id, project_name)
    out: dict[str, Any] = {"project": name, "fo_id": fo_id, "refreshed": False}
    if fo["status"] != "plan_review":
        return {**out, "reason": f"{fo_id} is {fo['status']}"}
    plan = db.from_json(fo.get("plan"), {}) or {}
    qid = fo.get("plan_question_id")
    if not (plan.get("design_doc") and plan.get("design_doc_content") and qid):
        return {**out, "reason": "no spec under review"}
    neo = NeoStore()
    try:
        q = neo.get(qid)
    finally:
        neo.close()
    # `answered` means the decision is taken; the verdict is on its way through the drain
    if q is None or q["status"] == "answered":
        return {**out, "reason": "the review is decided"}

    found = landing.committed_text(path, _planner_work_order(path, fo),
                                   plan["design_doc"])
    if found is None:
        # §7 rung 3
        log.warning("[%s] %s: no committed copy of %s on any rung — the plan under "
                    "review keeps the spec it was submitted with", name, fo_id,
                    plan["design_doc"])
        return {**out, "reason": "nothing readable"}
    text, source = found
    if (hashlib.sha256(text.encode()).hexdigest()
            == hashlib.sha256(str(plan["design_doc_content"]).encode()).hexdigest()):
        return {**out, "reason": "unchanged"}

    if q["status"] in ("escalated", "failed"):
        # The user holds this question
        store = ProjectStore(path)
        try:
            store.flag_feature_attention(
                fo_id, "the spec has been revised since this plan was reviewed — reject "
                       "it and let the planner resubmit")
        finally:
            store.close()
        return {**out, "reason": "the user holds the question"}

    problems = plans.spec_problems(plan, text)
    if problems:
        # The committed revision broke the plan
        feedback = ("the spec was revised on " + source + " and the plan no longer fits "
                    "it:\n  - " + "\n  - ".join(problems))
        review_plan(fo_id, accept=False, feedback=feedback, decided_by="os",
                    project_name=name)
        return {**out, "reason": "rejected", "problems": problems}

    plan["design_doc_content"], plan["design_doc_source"] = text, source
    planner_id = fo.get("plan_wo_id") or fo_id
    q2 = _ask_plan_review(name, fo, plan, planner_id, source,
                          why=f"the spec was revised on {source}")
    store = ProjectStore(path)
    try:
        store.update_feature_order(fo_id, plan=db.to_json(plan),
                                   plan_question_id=q2["id"])
        if fo.get("plan_wo_id"):
            store.add_event(fo["plan_wo_id"], "plan_spec_refreshed", {
                "feature_order": fo_id, "source": source,
                "was_question": qid, "neo_question_id": q2["id"],
            })
    finally:
        store.close()
    log.info("[%s] %s: the spec moved to %s — re-asked as question %s", name, fo_id,
             source, q2["id"])
    return {**out, "refreshed": True, "source": source, "neo_question_id": q2["id"],
            "superseded": qid}


def review_plan(fo_id: str, accept: bool = True, feedback: str = "",
                decided_by: str = "user",
                project_name: str | None = None) -> dict[str, Any]:
    """Release a submitted plan, or send it back"""
    from . import evidence, plans, specs
    from .neo_store import NeoStore

    name, path, fo = find_feature_order(fo_id, project_name)
    _require_kind(fo, "feature", "jarvis io review")
    if fo["status"] != "plan_review":
        raise OpsError(f"{fo_id} is {fo['status']}, not awaiting a plan review")
    if not accept and not feedback.strip():
        raise OpsError(
            "a rejection needs feedback — the planner sees only your reason, and "
            "without it the revision is a guess"
        )
    plan = db.from_json(fo.get("plan"), None)
    if not plan:
        raise OpsError(f"{fo_id} has no stored plan to review")

    store = ProjectStore(path)
    manager: dict[str, Any] | None = None
    try:
        if accept:
            children = store.create_plan_children(
                fo_id, plans.creation_order(plan["children"]),
                manager=validation_enabled(name))
            manager = store.manager_work_order(fo_id)
            # THE FEATURE'S BASE, recorded at the only moment it is knowable: the default branch's head just...
            store.set_feature_status(fo_id, "executing",
                                     base_sha=evidence.default_branch_head(path) or None)
            store.clear_feature_attention(fo_id)
            # The feature's own agent type, built from the spec's `Agent profile` appendix
            specs.install_agent(path, fo_id, str(plan.get("summary") or fo["title"]),
                                str(plan.get("design_doc_content") or ""))
        else:
            children = []
            store.set_feature_status(fo_id, "planning")
            store.clear_feature_attention(fo_id)
        if fo.get("plan_wo_id"):
            store.add_event(fo["plan_wo_id"], "plan_reviewed", {
                "feature_order": fo_id, "accepted": accept, "by": decided_by,
                "reason": feedback, "children": [c["id"] for c in children],
            })
    finally:
        store.close()

    # Close the review question whichever way it went
    if fo.get("plan_question_id"):
        neo = NeoStore()
        try:
            q = neo.get(fo["plan_question_id"])
            if q and q["status"] in ("queued", "answering", "escalated"):
                neo.record_answer(q["id"], "APPROVED" if accept else "REJECTED",
                                  answered_by=decided_by, reason=feedback)
        finally:
            neo.close()

    out = {"project": name, "fo_id": fo_id, "accepted": accept, "by": decided_by,
           "status": "executing" if accept else "planning",
           "children": [{"id": c["id"], "title": c["title"],
                         "depends_on": db.from_json(c["depends_on"], [])}
                        for c in children]}
    # Only when one exists, so a release with validation off returns the dict it always did
    if manager:
        out["manager"] = manager["id"]
    if not accept and fo.get("plan_wo_id"):
        try:
            out["delivered"] = send_message(
                fo["plan_wo_id"],
                f"The plan for {fo_id} was sent back by {decided_by}. Revise it and "
                f"resubmit with `jarvis fo plan {fo_id} --from-file <file>`.\n\n"
                f"Reason: {feedback}",
                source="jarvis", project_name=name,
            )
        except OpsError as e:
            out["delivery_error"] = str(e)
    return out


def feature_spec(fo_id: str, project_name: str | None = None,
                 section: str | None = None) -> dict[str, Any]:
    """The document the OS HOLDS for this feature — `jarvis fo spec` (docs/superpowers/specs/2026-09-28-a-feature-spec-you-can-open.md)"""
    from . import sections
    from .ui import markdown

    name, _path, fo = find_feature_order(fo_id, project_name)
    plan = db.from_json(fo.get("plan"), {}) or {}
    content = str(plan.get("design_doc_content") or "")
    repo_path = str(plan.get("design_doc") or "")
    if not (content and repo_path):
        raise OpsError(
            f"{fo_id} is {fo['status']} and holds no spec — a feature's document is "
            f"snapshotted when its planner submits a plan"
        )
    out = {"project": name, "fo_id": fo_id, "repo_path": repo_path,
           "source": str(plan.get("design_doc_source") or ""), "content": content}
    if section:
        text = sections.extract_section(content, section)
        if text is None:
            # The same courtesy `plans.spec_problems` gives a planner: name what exists rather than leaving the...
            names = [t for t, _s in markdown.anchors(content)]
            headings = ", ".join(names[:12]) + ("…" if len(names) > 12 else "")
            raise OpsError(
                f"{section!r} matches no section of {repo_path}. It carries: "
                f"{headings or 'no headings at all'}"
            )
        out["section"] = section
        out["content"] = text
    return out


def rebuild_feature_agent(fo_id: str,
                          project_name: str | None = None) -> dict[str, Any]:
    """Rewrite a feature's agent type from its stored spec"""
    from . import specs

    name, path, fo = find_feature_order(fo_id, project_name)
    plan = db.from_json(fo.get("plan"), {}) or {}
    content = str(plan.get("design_doc_content") or "")
    if not content:
        raise OpsError(
            f"{fo_id} has no spec snapshot to build an agent from — it was planned "
            f"before the spec became the feature's artifact, or its plan was never "
            f"submitted"
        )
    problems = specs.profile_problems(content)
    if problems:
        raise OpsError("; ".join(problems))
    agent = specs.install_agent(path, fo_id, str(plan.get("summary") or fo["title"]),
                                content)
    if not agent:
        raise OpsError(f"the agent type for {fo_id} could not be written — see the log")
    return {"project": name, "fo_id": fo_id, "agent": agent,
            "dir": str(specs.agent_root(path, fo_id)),
            "spec": str(plan.get("design_doc") or "")}


def cancel_feature_order(fo_id: str, project_name: str | None = None) -> dict[str, Any]:
    """The user stopping a feature order, and everything it has running"""
    name, path, fo = find_feature_order(fo_id, project_name)
    if fo["status"] in FO_TERMINAL_STATUSES:
        raise OpsError(f"{fo_id} is already {fo['status']}")
    store = ProjectStore(path)
    try:
        owned = store.feature_children(fo_id, kinds=WORKER_CHILDREN)
        if fo.get("plan_wo_id"):
            try:
                owned.append(store.get_work_order(fo["plan_wo_id"]))
            except KeyError:
                pass
        # The manager is not a child (`feature_children` filters to `kind='worker'`
        manager = store.manager_work_order(fo_id)
        if manager:
            owned.append(manager)
        stop_ids = [w["id"] for w in owned if w["status"] in OPEN_STATUSES]
        store.set_feature_status(fo_id, "cancelled")
        store.clear_feature_attention(fo_id)
    finally:
        store.close()
    # A feature cancelled while its plan was still under review leaves that review with nothing to decide...
    if fo.get("plan_question_id"):
        from .neo_store import NeoStore

        neo = NeoStore()
        try:
            neo.supersede(fo["plan_question_id"], "SUPERSEDED — feature order cancelled",
                          f"{fo_id} was cancelled, so its plan will not be released "
                          f"whatever this review concluded")
        finally:
            neo.close()
    for wo_id in stop_ids:
        cancel(wo_id)
    return {"project": name, "fo_id": fo_id, "title": fo["title"],
            "status": "cancelled", "cancelled_work_orders": stop_ids}


#: The most of a `--fix` that becomes the corrective child's title
FIX_TITLE_CHARS = 120


def revive_feature_manager(store: ProjectStore, fo_id: str,
                           why: str) -> dict[str, Any] | None:
    """Give an `executing` feature back the manager its settlement closed (#264)"""
    try:
        feature = store.get_feature_order(fo_id)
    except KeyError:
        return None
    if feature["status"] != "executing":
        return None
    manager = store.manager_work_order(fo_id)
    if manager is None or manager["status"] in OPEN_STATUSES:
        return None
    store.set_status(manager["id"], "idle")
    store.clear_attention(manager["id"])
    # The exact mirror of `_close_feature_manager`'s `feature_settled`
    store.add_event(manager["id"], "manager_revived",
                    {"feature_order": fo_id, "was": manager["status"], "why": why})
    return store.get_work_order(manager["id"])


def lower_settled_feature_holds(store: ProjectStore,
                                children: list[dict[str, Any]]) -> list[str]:
    """Take down the hold a child carried while its feature was settled"""
    lowered = []
    for child in children:
        fresh = store.get_work_order(child["id"])
        if not fresh["needs_attention"] or true_blockers(store, fresh):
            continue
        store.clear_attention(fresh["id"])
        lowered.append(fresh["id"])
    return lowered


def resume_feature_order(fo_id: str, fix: str = "",
                         project_name: str | None = None) -> dict[str, Any]:
    """`jarvis fo resume` — put a failed feature order back to work (docs/superpowers/specs/2026-08-29-feature-order-resume.md)"""
    from .invariants import dead_feature_children

    name, path, fo = find_feature_order(fo_id, project_name)
    # An improvement order has no children to revive — §2.4 of the improvement-orders spec
    _require_kind(fo, "feature", "jarvis io show")
    if fo["status"] != "failed":
        raise OpsError(
            f"{fo_id} is {fo['status']}, not failed — `fo resume` revives a feature a "
            f"child killed. Nothing to resume."
        )
    store = ProjectStore(path)
    try:
        children = store.feature_children(fo_id, kinds=WORKER_CHILDREN)
        dead = dead_feature_children(store, children)
        if dead:
            store.supersede_children(fo_id, [c["id"] for c in dead], note=fix)
        store.set_feature_status(fo_id, "executing")
        store.clear_feature_attention(fo_id)
        # BEFORE the `--fix` child is filed
        revive_feature_manager(store, fo_id, why="jarvis fo resume")
        lower_settled_feature_holds(store, children)
        child = None
        if fix.strip():
            title = " ".join(fix.split())[:FIX_TITLE_CHARS]
            # `store.create_work_order`, not `ops.create_work_order`: the latter refuses a parent that is not...
            child = store.create_work_order(
                title=title, description=fix, origin="jarvis", kind="worker",
                parent_id=fo_id,
            )
        return {"project": name, "fo_id": fo_id, "title": fo["title"],
                "status": "executing",
                "superseded": [c["id"] for c in dead],
                "fix_wo_id": child["id"] if child else None}
    finally:
        store.close()


# -- Neo (OS answerer agent)

#: The most ALL referenced sections together may ride to Neo
SECTION_SNAPSHOT_CHARS = 6000


def _section_candidates(path: Path, wo: dict[str, Any], ref_path: str) -> list[Path]:
    """Every place this worker could be naming, in order of authority"""
    candidates = []
    if Path(ref_path).is_absolute():
        candidates.append(Path(ref_path))
    else:
        if wo.get("worktree"):
            candidates.append(path / ".claude" / "worktrees" / wo["worktree"] / ref_path)
        candidates.append(path / ref_path)
        if wo.get("parent_id"):
            candidates.append(path / ".jarvis" / "features" / wo["parent_id"]
                              / Path(ref_path).name)
    return candidates


def _resolve_section(path: Path, wo: dict[str, Any], ref_path: str,
                     which: str) -> str | None:
    """The referenced section's text, read from wherever this worker can see the file"""
    from . import sections

    for candidate in _section_candidates(path, wo, ref_path):
        if candidate.is_file():
            section = sections.extract_section(candidate.read_text(), which)
            if section is not None:
                return section   # verbatim; `_section_blocks` owns the TOTAL budget
    return None


def _water_fill(sizes: list[int], total: int) -> list[int]:
    """What each block may spend of one `total`: equal share is a FLOOR"""
    allowed = list(sizes)
    open_ = set(range(len(sizes)))
    budget = total
    while open_:
        share = budget // len(open_)
        under = [i for i in open_ if sizes[i] <= share]
        if not under:
            for i in open_:
                allowed[i] = share
            break
        for i in under:
            allowed[i] = sizes[i]
            budget -= sizes[i]
            open_.discard(i)
    return allowed


#: The in-text form, shown in every refusal below
SECTION_FORM_HINT = 'from section 3 of design doc "docs/superpowers/specs/feature.md": …'


def _section_blocks(path: Path, wo: dict[str, Any],
                    text: str) -> list[tuple[str, str, str]]:
    """Every design-doc section this text cites, resolved: `(path, which, section)` (#102)"""
    from . import sections

    refs = sections.find_refs(text)
    bare = sections.unreferenced_docs(text)
    if bare:
        raise OpsError(
            f"that text names {bare[0]!r} without naming a section of it, so there is "
            f"nothing to resolve. Cite the section in the sentence — "
            f"`{SECTION_FORM_HINT}` — and that section alone is delivered.")
    out = []
    for ref_path, which in refs:
        section = _resolve_section(path, wo, ref_path, which)
        if section is None:
            exists = any(c.is_file() for c in _section_candidates(path, wo, ref_path))
            why = (f"that document has no such heading — `jarvis spec toc {ref_path}` "
                   f"lists the ones it has"
                   if exists else
                   "no such file in your worktree or the project tree — check the path")
            raise OpsError(f"section {which!r} of {ref_path!r} does not resolve: {why}. "
                           f"The section is what Neo reads, so nothing is recorded until "
                           f"the reference resolves.")
        out.append((ref_path, which, section))
    allowed = _water_fill([len(s) for _, _, s in out], SECTION_SNAPSHOT_CHARS)
    return [(ref_path, which,
             section if len(section) <= cap else
             section[:cap] + f"\n[… section truncated — {cap:,} of {len(section):,} "
                             f"characters; full section: {ref_path} § {which}]")
            for (ref_path, which, section), cap in zip(out, allowed)]


#: The in-text form of a reference to an earlier question or a sibling assumption (spec §4)
REF_FORM_HINT = "per Q1234 / per assumption #2"


def _cited_blocks(project: str, path: Path, wo: dict[str, Any], text: str) -> str:
    """Every in-text question or sibling reference this text names, RESOLVED"""
    from . import autoreview
    from .neo_store import NeoStore

    if not (autoreview.cited_question_ids(text) or autoreview.cited_sibling_ns(text)):
        return ""
    store = ProjectStore(path)
    try:
        siblings = store.all_assumptions(wo["id"]) or []
    finally:
        store.close()
    neo = NeoStore()
    try:
        problem = autoreview.cited_ref_problem(neo, wo["id"], text, siblings)
        if problem:
            raise OpsError(
                f"{problem}. The reference is what whoever answers reads, so nothing "
                f"is recorded until it resolves. The form is in-text and "
                f"keyword-anchored — {REF_FORM_HINT}.")
        return autoreview.cited_context(neo, wo["id"], text, siblings,
                                        cfg=validation_config(project))
    finally:
        neo.close()


def ask_question(wo_id: str, question: str, project_name: str | None = None) -> dict[str, Any]:
    """(Workers) queue a question for Neo instead of stalling on the user: waiting_input with NO..."""
    from .neo_store import NeoStore

    name, path, wo = find_work_order(wo_id, project_name)
    cap = _concision_cap(name, "question_max_chars",
                         concision.DEFAULT_QUESTION_MAX_CHARS)
    if cap and len(question) > cap:
        raise OpsError(
            f"that question is {len(question)} characters; the cap is "
            f"{cap}. A question to Neo is ONE QUESTION, in one paragraph — the "
            f"decision, the options, your recommendation — arguing from a design "
            f"artifact it references in-text, e.g. `from section 3 of design doc "
            f"\"docs/superpowers/specs/feature.md\": …`. The referenced section is delivered to "
            f"whoever answers, alongside your paragraph; you do not need to paste it. "
            f"Split it: a second question is a second `jarvis wo ask`, and cite this "
            f"one in that one by its `Q<id>`. Nothing was recorded."
        )

    context = f"{wo['title']}\n{(wo.get('description') or '')[:800]}"
    for ref_path, which, section in _section_blocks(path, wo, question):
        context += f"\n\nReferenced artifact — {ref_path} § {which}:\n{section}"
    cited = _cited_blocks(name, path, wo, question)
    if cited:
        context += f"\n\nReferences this question names:\n{cited}"
    neo = NeoStore()
    try:
        q = neo.ask(name, wo_id, question, context=context)
    finally:
        neo.close()
    store = ProjectStore(path)
    try:
        # The text, not just the id: the question lives in Neo's separate DB
        store.add_event(wo_id, "question_asked",
                        {"neo_question_id": q["id"], "question": question})
        if wo["status"] == "running":
            store.set_status(wo_id, "waiting_input")
    finally:
        store.close()
    out = {"project": name, "wo_id": wo_id, "question_id": q["id"],
           "note": "queued for Neo — end your turn; the answer arrives as your next user turn"}
    if len(question) > QUESTION_WARN_CHARS:
        out["warning"] = (
            f"that question is {len(question)} characters — aim for one paragraph, and "
            f"reference the design artifact section it argues from in-text instead of "
            f"pasting context (the cap is {cap or QUESTION_MAX_CHARS})"
        )
    return out


def neo_status() -> dict[str, Any]:
    from .neo_store import NeoStore
    neo = NeoStore()
    try:
        return neo.counts()
    finally:
        neo.close()


def neo_export() -> dict[str, list[dict[str, Any]]]:
    """Neo's whole ledger as one stable document — see `NeoStore.export`"""
    from .neo_store import NeoStore

    neo = NeoStore()
    try:
        return neo.export()
    finally:
        neo.close()


def validate_seat(seat: str) -> None:
    """Refuse a seat name no learning may carry, BEFORE anything is written"""
    from .neo_store import LEARNING_SCOPES

    if seat and seat not in LEARNING_SCOPES:
        raise OpsError(f"unknown seat {seat!r} — the seats are: "
                       f"{', '.join(LEARNING_SCOPES)}")


def neo_review(question_id: int, approved: bool, feedback: str = "",
               seat: str = "") -> dict[str, Any]:
    """Review one of Neo's answers"""
    from . import neo as neo_mod
    from .neo_store import NeoStore

    # Every refusal below happens before the first write: a rejected review must leave the question...
    validate_seat(seat)
    if seat and approved:
        raise OpsError("--seat scopes a correction, and an approval records no learning "
                       "to scope — approve it, or say what Neo should have answered")
    if not approved and not feedback.strip():
        raise OpsError("a correction needs feedback — what should Neo have said?")
    neo = NeoStore()
    try:
        q = neo.get(question_id)
        if q is None:
            raise OpsError(f"neo question {question_id} not found")
        if q["status"] != "answered":
            raise OpsError(f"neo question {question_id} is {q['status']}, not answered")
        if seat:
            opined = [o["seat"] for o in neo.opinions(question_id)]
            if not opined:
                raise OpsError(
                    f"no panel ran on neo question {question_id}, so there is no "
                    f"{seat!r} seat to correct — Neo answered it single-agent. Drop "
                    f"--seat to teach every seat, or use `jarvis neo learn`.")
            if seat not in opined:
                raise OpsError(
                    f"the {seat!r} seat did not opine on neo question {question_id}, so "
                    f"it never saw the question — the seats that did: "
                    f"{', '.join(opined)}. Drop --seat to teach every seat.")
        q = neo.review(question_id, approved, feedback)
        learning = None
        if not approved:
            learning = neo.add_learning(
                neo_mod.learning_from_review(q, feedback),
                project=q["project"], source="review", question_id=question_id,
                seat=seat, wo_id=q["wo_id"] or "", origin_kind="question",
                origin_id=str(question_id),
            )
    finally:
        neo.close()
    forwarded = False
    # THREE KINDS HAVE NO WORKER TO FORWARD A CORRECTION TO (#240)
    if not approved and q.get("kind") in ("triage", "assumption", "findings"):
        return {"question_id": question_id, "review": "corrected",
                "learning_recorded": learning is not None,
                "learning_seat": seat or "all seats",
                "forwarded_to_worker": False}
    if not approved:
        try:
            _, _, wo = find_work_order(q["wo_id"], q["project"])
            if wo["status"] not in ("completed", "failed", "cancelled"):
                send_message(
                    q["wo_id"],
                    f"Correction from the user on Neo's earlier answer "
                    f"(\"{(q.get('answer') or '')[:120]}\"): {feedback}",
                    source="jarvis", project_name=q["project"],
                )
                forwarded = True
        except OpsError:
            pass
    return {"question_id": question_id,
            "review": "approved" if approved else "corrected",
            "learning_recorded": learning is not None,
            "learning_seat": seat or "all seats",
            "forwarded_to_worker": forwarded}


def _alarm_review_hint(q: dict[str, Any]) -> str:
    """The command that really decides an alarm question, naming the alarm when it can"""
    try:
        _name, path, _wo = find_work_order(q["wo_id"], q.get("project"))
        store = ProjectStore(path)
        try:
            alarm = store.alarm_for_question(q["id"])
        finally:
            store.close()
    except (OpsError, KeyError):
        alarm = None
    return (f"review it with: jarvis alarms review {alarm['id']}" if alarm
            else "review it with: jarvis alarms review <al-id>")


def _triage_promote_hint(q: dict[str, Any]) -> str:
    """The command that really decides a bug-priority question"""
    from . import issues

    url = (issues.triage_payload(q) or {}).get("issue_url") or "<issue-url>"
    return (f"it is on the tracker, so start work on it with: "
            f"{issues.START_COMMAND.format(url=url)}")


def neo_answer_escalated(question_id: int, answer: str) -> dict[str, Any]:
    """The user answers a question Neo escalated"""
    from .neo_store import NeoStore

    neo = NeoStore()
    try:
        q = neo.get(question_id)
        if q is None:
            raise OpsError(f"neo question {question_id} not found")
        if q["status"] not in ("escalated", "failed", "queued"):
            raise OpsError(f"neo question {question_id} is {q['status']} — "
                           "only escalated/failed/queued questions take a user answer")
        # AN ALARM QUESTION HAS NO WORKER TO ANSWER (docs/superpowers/specs/2026-08-31-the-supervisor.md)
        if q.get("kind") == "alarm":
            raise OpsError(f"neo question {question_id} is a cost alarm, and answering "
                           f"it would message the worker mid-turn — "
                           f"{_alarm_review_hint(q)}")
        # AN ASSUMPTION QUESTION HAS NO WORKER TO ANSWER EITHER
        if q.get("kind") == "assumption":
            raise OpsError(
                f"neo question {question_id} is an assumption review, and the worker "
                f"that recorded it has finished — answering here would reopen it. "
                f"Decide the assumption instead: jarvis wo review {q['wo_id']} "
                f"[--reject] --feedback \"...\"")
        # A TRIAGE QUESTION HAS NO WORKER TO ANSWER EITHER (#240)
        if q.get("kind") == "triage":
            raise OpsError(f"neo question {question_id} is a bug-priority "
                           f"re-assessment, and no work order is waiting on it — "
                           f"{_triage_promote_hint(q)}")
        # Nor a findings one: the analyst has settled, and the decision is taken on the io
        if q.get("kind") == "findings":
            located = _finding_for_question(q)
            io_id, key = ((located[2]["id"], located[4]["key"]) if located
                          else ("<io-id>", "<key>"))
            raise OpsError(
                f"neo question {question_id} decides an improvement-order finding, and "
                f"no worker is waiting on it — decide the finding instead: "
                f"jarvis io review {io_id} --accept {key}, or "
                f"jarvis io review {io_id} --reject {key} --feedback \"...\"")
        # Nor a Morpheus one: the PR is decided on its item, not by messaging anyone
        if q.get("kind") == "validation":
            from . import morpheus

            item = morpheus.item_of_question(q) or "<mv-id>"
            raise OpsError(
                f"neo question {question_id} asks you to rule on a shipped PR's production "
                f"check ({item}), and no worker is waiting for a reply. If it works in "
                f"production: jarvis morpheus waive {item} --reason \"...\". To run the "
                f"check again (after fixing what made it fail): "
                f"jarvis morpheus requeue {item}. If it is broken and needs a fix: "
                f"jarvis morpheus confirm {item} --fix \"...\". Each closes this question.")
        neo.record_answer(question_id, answer, answered_by="user")
        neo.review(question_id, approved=True)  # user-authored ⇒ nothing to review
    finally:
        neo.close()
    # A relay surface like `wo send`: this text IS the user's
    delivery = send_message(q["wo_id"], f"[Answer from the user] {answer}",
                            project_name=q["project"], relay=True)
    # The escalation is handled — release the work order from the attention list
    try:
        _, path, _ = find_work_order(q["wo_id"], q["project"])
        store = ProjectStore(path)
        try:
            store.clear_attention(q["wo_id"])
            store.add_event(q["wo_id"], "escalation_answered",
                            {"neo_question_id": question_id})
            invariants.end_wait_if_nothing_is_out(store, q["wo_id"])
        finally:
            store.close()
    except OpsError:
        pass
    return {"question_id": question_id, "delivery": delivery}


# -- gates (privileged-action approvals)

def _project_gate_config(project_name: str):
    """The project's gate config from the catalog, or an empty one"""
    from .gates import GateConfig

    try:
        catalog = resolve_catalog()
    except (OpsError, CatalogError):
        return GateConfig()
    for spec in catalog.projects:
        if spec.name == project_name:
            return spec.gates
    return GateConfig()


#: Refused when `JARVIS_WO_ID` is unset
CONTEST_NEEDS_AN_OWNER = (
    "`jarvis gate contest` is a worker's exit from its own block and needs JARVIS_WO_ID, "
    "which is set only inside a dispatched worker's session. An upheld contest clears the "
    "command and teaches the recogniser fleet-wide, so it is not something to run on "
    "another unit's behalf. To rule on a request yourself: `jarvis gate dismiss <id> "
    "--reason \"...\"` if the recogniser was wrong, `jarvis gate deny <id> --reason "
    "\"...\"` if it was not."
)


def resolve_gate_target(target: str, command: str | None = None,
                        project_name: str | None = None,
                        caller_wo_id: str | None = None,
                        require_caller: bool = False) -> tuple[str, str]:
    """`(wo_id, command)` from either spelling of a gate exit: `<wo> "<cmd>"` or `<id>`"""
    if require_caller and not caller_wo_id:
        raise OpsError(CONTEST_NEEDS_AN_OWNER)
    if command is not None:
        if caller_wo_id and target != caller_wo_id:
            raise OpsError(
                f"{target} is not your work order ({caller_wo_id}) — a gate exit acts on "
                f"the unit that was blocked, and this one was not."
            )
        return target, command
    if not target.strip().isdigit():
        raise OpsError(
            f"{target!r} is neither a request number nor a work order with a command "
            f"after it. Either `jarvis gate <verb> <request-number> …` (the number the "
            f"block printed) or `jarvis gate <verb> <wo-id> \"<the exact command>\" …`."
        )
    name, _, approval = _find_approval(int(target.strip()), project_name)
    if caller_wo_id and approval["wo_id"] != caller_wo_id:
        raise OpsError(
            f"request {target.strip()} belongs to {approval['wo_id']} (project {name!r}), "
            f"not to you ({caller_wo_id}) — refusing, because a contest amends the "
            f"standing row and this one is not yours to re-frame. Re-read the number in "
            f"your own block message: `jarvis gate list --wo {caller_wo_id}` lists the "
            f"requests filed against this work order."
        )
    return approval["wo_id"], approval["command"]


def request_gate_approval(wo_id: str, command: str, why: str = "", evidence: str = "",
                          project_name: str | None = None) -> dict[str, Any]:
    """(Workers) ask for permission to run a privileged command, making the case for it"""
    from . import gates
    from .neo_store import NeoStore

    name, path, wo = find_work_order(wo_id, project_name)
    # No case, no request
    if not why.strip() and not evidence.strip():
        raise OpsError(
            "a gate request needs a case — pass --why (and --evidence: the PR, the test "
            "results, the checks). The reviewer sees only what you write here, and a "
            "request with nothing in it can only be refused."
        )
    config = _project_gate_config(name)
    if not config:
        raise OpsError(
            f"project {name!r} has no gates enabled, so there is nothing to request. "
            f"Either the command needs no approval, or the catalog needs a `gates` "
            f"entry for this project."
        )
    action = gates.classify(command, config)
    if action is None:
        raise OpsError(
            f"that command does not trip any gate enabled for {name!r} "
            f"(enabled: {sorted(config.enabled)}) — run it directly, no approval needed."
        )

    store = ProjectStore(path)
    try:
        existing = store.latest_approval_for(wo_id, action.kind, action.command)
        if existing and existing["status"] in (gates.AWAITING_CASE, "pending"):
            # The case goes onto the STANDING request
            held = existing["status"] == gates.AWAITING_CASE
            neo = NeoStore()
            try:
                gates.amend_request(store, neo, wo, action, existing,
                                    justification=why, evidence=evidence)
                if held:
                    # The case has arrived, so the request can finally be seen
                    question = gates.queue_for_review(store, neo, name, wo, action,
                                                      store.get_approval(existing["id"]))
                else:
                    question = (neo.get(existing["neo_question_id"])
                                if existing["neo_question_id"] else None)
            finally:
                neo.close()
            if held:
                note = (f"your case was filed and request {existing['id']} is now under "
                        f"review — END YOUR TURN; the verdict arrives as your next "
                        f"user turn")
            else:
                read_already = question is not None and question["status"] != "queued"
                note = (f"your case was attached to request {existing['id']}, which was "
                        f"already under review — no second request was filed. "
                        + ("The reviewer may already have read the earlier text, so a "
                           "verdict that ignores this case is not a refusal of it: "
                           "address the reason and request afresh. "
                           if read_already else "")
                        + "END YOUR TURN; the verdict arrives as your next user turn")
            return {"project": name, "wo_id": wo_id, "approval_id": existing["id"],
                    "kind": action.kind, "status": "pending",
                    "neo_question_id": question["id"] if question else None,
                    "note": note}
        grant = store.usable_grant(wo_id, action.kind, action.command)
        if grant:
            return {"project": name, "wo_id": wo_id, "approval_id": grant["id"],
                    "kind": action.kind, "status": "approved",
                    "note": "already approved — run the command as written"}
        neo = NeoStore()
        try:
            approval, question = gates.file_request(
                store, neo, name, wo, action, justification=why, evidence=evidence,
            )
        finally:
            neo.close()
    finally:
        store.close()
    return {"project": name, "wo_id": wo_id, "approval_id": approval["id"],
            "kind": action.kind, "command": action.command,
            "neo_question_id": question["id"], "status": "pending",
            "note": "queued for review — END YOUR TURN; the verdict arrives as your "
                    "next user turn"}


def contest_gate_match(wo_id: str, command: str, why: str,
                       project_name: str | None = None) -> dict[str, Any]:
    """(Workers) dispute a gate MATCH: this command performs no privileged action (docs/superpowers/specs/2026-09-12-contesting-a-gate-match.md §1.)"""
    from . import gates
    from .neo_store import NeoStore

    name, path, wo = find_work_order(wo_id, project_name)
    if not why.strip():
        raise OpsError(
            "a contest needs an argument — pass --why, saying what the command actually "
            "does and where the matched text sits (a grep pattern, a commit message, a "
            "heredoc). The reviewer sees only what you write, and a contest with nothing "
            "in it can only be denied."
        )
    config = _project_gate_config(name)
    if not config:
        raise OpsError(
            f"project {name!r} has no gates enabled, so nothing could have matched this "
            f"command — there is nothing to contest."
        )
    action = gates.classify(command, config)
    if action is None:
        # Naming the command checked matters: a grant is scoped to an exact string
        raise OpsError(
            f"that command trips no gate enabled for {name!r} "
            f"(enabled: {sorted(config.enabled)}) — run it directly. If a gate really "
            f"fired, the string here is not the string that was blocked: copy it exactly "
            f"from `jarvis gate list --wo {wo_id}`."
        )

    store = ProjectStore(path)
    try:
        grant = store.usable_grant(wo_id, action.kind, action.command)
        if grant is not None:
            return {"project": name, "wo_id": wo_id, "approval_id": grant["id"],
                    "kind": action.kind, "status": grant["status"],
                    "note": "already cleared — run the command as written"}
        existing = store.latest_approval_for(wo_id, action.kind, action.command)
        # Open and not yet contested, and nothing else — reviewer-shopping otherwise (kn-76b155a0)
        if existing and existing["status"] == "pending" and existing["contested"]:
            raise OpsError(
                f"request {existing['id']} is already contested and in front of a "
                f"reviewer — one claim, one review. To add to the argument, send it to "
                f"the reviewer rather than re-filing it; to see it as they do, "
                f"`jarvis gate show {existing['id']}`."
            )
        if existing and existing["status"] == "denied":
            raise OpsError(
                f"request {existing['id']} was already DENIED by "
                f"{existing['decided_by'] or 'a reviewer'}: "
                f"{existing['decision_reason'] or 'no reason recorded'}. A reviewer has "
                f"ruled that this command does perform the action, so contesting it again "
                f"asks the same question of a second reviewer. Address the reason instead."
            )
        neo = NeoStore()
        try:
            if existing and existing["status"] in (gates.AWAITING_CASE, "pending"):
                # Onto the STANDING row, never a second one (kn-76b155a0)
                held = existing["status"] == gates.AWAITING_CASE
                approval = gates.amend_request(store, neo, wo, action,
                                               store.mark_contested(existing["id"]),
                                               justification=why)
                if held:
                    gates.queue_for_review(store, neo, name, wo, action, approval)
            else:
                # No hold: a contest IS the case the reviewer needs — spec §1
                approval, _ = gates.file_request(store, neo, name, wo, action,
                                                 justification=why, contested=True)
        finally:
            neo.close()
    finally:
        store.close()
    return {
        "project": name, "wo_id": wo_id, "approval_id": approval["id"],
        "kind": action.kind, "command": action.command, "status": "pending",
        "contested": True,
        "note": ("contested — a reviewer will decide whether the recogniser was wrong. "
                 "It cannot authorise anything, so the outcomes are DISMISSED (run the "
                 "command as written) or DENIED (it really does perform the action). "
                 "END YOUR TURN; the verdict arrives as your next user turn"),
    }


def decide_gate(approval_id: int, verdict: str, reason: str = "",
                project_name: str | None = None) -> dict[str, Any]:
    """(User) rule on a gate directly, whatever Neo did or didn't say"""
    from . import gates

    from .neo_store import NeoStore

    if verdict not in gates.VERDICTS:
        raise OpsError(f"unknown verdict {verdict!r} — expected one of "
                       f"{list(gates.VERDICTS)}")
    # A denial needs a reason because the worker has to act on it
    if verdict == "denied" and not reason.strip():
        raise OpsError("a denial needs a reason — the worker acts on it")
    if verdict == "dismissed" and not reason.strip():
        raise OpsError("a dismissal needs a reason — it is the report on what the gate's "
                       "recogniser got wrong, and the only note attached to the "
                       "false-positive count")

    name, path, approval = _find_approval(approval_id, project_name)
    # `awaiting_case` decides too
    if approval["status"] not in ("pending", gates.AWAITING_CASE):
        raise OpsError(
            f"approval {approval_id} is already {approval['status']}"
            + (f" (by {approval['decided_by']})" if approval["decided_by"] else "")
        )
    # Refused here rather than coerced, because there is a person to tell
    if approval["contested"] and verdict == "approved":
        raise OpsError(
            f"approval {approval_id} is a CONTEST, not a request for permission — the "
            f"worker argued this command performs no privileged action, and made no case "
            f"for performing one. Dismiss it if it is right (`jarvis gate dismiss "
            f"{approval_id} --reason \"...\"`), deny it if it is wrong. To authorise the "
            f"action itself, the worker files `jarvis gate request` with a case."
        )
    store = ProjectStore(path)
    central = CentralStore()
    try:
        gates.apply_decision(store, approval_id, verdict=verdict,
                             reason=reason or "approved by the user", decided_by="user",
                             central=central, project=name)
        # The flag this takes down is the one an ESCALATED gate raised, and the user has just answered it
        if approval["kind"] != gates.SELF_HEAL:
            store.clear_attention(approval["wo_id"])
    finally:
        central.close()
        store.close()
    # The user has decided, so Neo's queued question (if it is still waiting) is moot
    neo = NeoStore()
    try:
        qid = approval["neo_question_id"]
        q = neo.get(qid) if qid else None
        if q and q["status"] in ("queued", "answering", "escalated", "failed"):
            neo.record_answer(qid, verdict.upper(), answered_by="user", reason=reason)
            neo.review(qid, approved=True)  # user-authored ⇒ nothing to review
    finally:
        neo.close()
    return {"project": name, "wo_id": approval["wo_id"], "approval_id": approval_id,
            "decision": verdict,
            "command": approval["command"],
            "delivery": "jarvisd delivers the verdict when the worker is idle"}


def _find_approval(approval_id: int, project_name: str | None = None
                   ) -> tuple[str, Path, dict[str, Any]]:
    """Locate an approval by id across registered projects"""
    paths = registered_project_paths()
    candidates = {project_name: paths[project_name]} if project_name else paths
    if project_name and project_name not in paths:
        raise OpsError(f"project {project_name!r} not registered")
    hits: list[tuple[str, Path, dict[str, Any]]] = []
    for name, path in candidates.items():
        if not path.is_dir():
            continue
        store = ProjectStore(path)
        try:
            approval = store.get_approval(approval_id)
        finally:
            store.close()
        if approval:
            hits.append((name, path, approval))
    if not hits:
        raise OpsError(f"approval {approval_id} not found in any registered project")
    if len(hits) > 1:
        raise OpsError(
            f"approval id {approval_id} exists in {[h[0] for h in hits]} — "
            f"disambiguate with --project"
        )
    return hits[0]


def list_gates(project_name: str | None = None, wo_id: str | None = None,
               pending_only: bool = False, include_request: bool = False
               ) -> list[dict[str, Any]]:
    """Approval requests across the fleet, newest first"""
    from . import gates

    paths = registered_project_paths()
    if project_name:
        if project_name not in paths:
            raise OpsError(f"project {project_name!r} not registered")
        paths = {project_name: paths[project_name]}
    out: list[dict[str, Any]] = []
    for name, path in paths.items():
        if not path.is_dir():
            continue
        store = ProjectStore(path)
        try:
            store.expire_approvals()
            rows = store.list_approvals(
                # `awaiting_case` counts as outstanding here even though no reviewer holds it: a request invisible on...
                wo_id, statuses=("pending", gates.AWAITING_CASE) if pending_only else None
            )
        finally:
            store.close()
        out.extend({**r, "project": name} for r in rows)
    out.sort(key=lambda r: r["ts"], reverse=True)
    # A held request is the one status whose next event is a CLOCK
    ttls = _case_ttl_seconds()
    for row in out:
        if row["status"] == gates.AWAITING_CASE:
            ttl = ttls.get(row["project"], gates.DEFAULT_CASE_TTL_SECONDS)
            row["case_ttl_seconds"] = ttl
            row["case_deadline"] = row["ts"] + ttl
    if include_request:
        from .neo_store import NeoStore
        neo = NeoStore()
        try:
            for row in out:
                qid = row["neo_question_id"]
                row["neo_question"] = neo.get(qid) if qid else None
        finally:
            neo.close()
    return out


def _case_ttl_seconds() -> dict[str, float]:
    """Each project's `gates.case_ttl_seconds`, empty when the catalog is unreadable"""
    try:
        catalog = resolve_catalog()
    except (OpsError, CatalogError):
        return {}
    return {spec.name: spec.gates.case_ttl_seconds
            for spec in catalog.projects if spec.gates}


def list_gate_rules(role: str | None = None, kind: str | None = None,
                    include_retired: bool = False) -> dict[str, Any]:
    """The rule base: what the OS believes is privileged, and what it has learned is not"""
    from .gate_rules import ROLES, Rule, RuleSet

    if role and role not in ROLES:
        raise OpsError(f"unknown role {role!r} — expected one of {list(ROLES)}")
    central = CentralStore()
    try:
        rows = central.gate_rules(role=role, kind=kind, include_retired=include_retired)
        live = RuleSet.load(central)
    finally:
        central.close()
    return {
        "rules": [{**r, "rendered": Rule.from_row(r).render()} for r in rows],
        "canary_failures": live.check_canaries(),
        "classifier": classifier_stats(),
    }


def classifier_stats() -> dict[str, int]:
    """How often the recogniser has been wrong, and how often nobody stayed to say so"""
    dismissed = abandoned = total = 0
    for _name, path in registered_project_paths().items():
        if not path.is_dir():
            continue
        store = ProjectStore(path)
        try:
            dismissed += store.dismissed_count()
            abandoned += store.abandoned_count()
            total += len(store.list_approvals())
        finally:
            store.close()
    return {"dismissed": dismissed, "abandoned": abandoned, "requests": total}


def retract_gate_rule(rule_id: str, reason: str) -> dict[str, Any]:
    """Retire a rule the user has overruled"""
    from .gate_rules import RuleSet

    if not reason.strip():
        raise OpsError("a retraction needs a reason — it is the only record of why the "
                       "OS stopped believing something it acted on")
    central = CentralStore()
    try:
        try:
            rule = central.retract_gate_rule(rule_id, reason.strip())
        except KeyError as e:
            raise OpsError(str(e)) from e
        except ValueError as e:
            raise OpsError(str(e)) from e
        failures = RuleSet.load(central).check_canaries()
    finally:
        central.close()
    return {"rule": rule, "canary_failures": failures,
            "note": ("retracted — it no longer applies, and the record keeps that it "
                     "once did")}


# -- the self-evolution registry (detectors and remedy rules; see rules.py)

#: What a fire counts as evidence FOR
_HIT_OUTCOMES = ("recorded", "proposed", "applied")


def _rule_entry(central: CentralStore, detector: dict[str, Any]) -> dict[str, Any]:
    """One detector as every surface renders it: the row"""
    from . import rules as rules_mod

    prose: str | None = None
    problems: list[str] = []
    try:
        prose = rules_mod.render_condition(
            rules_mod.parse_condition(detector["condition"]))
    except rules_mod.RulesError as e:
        problems = list(e.args[0]) if e.args and isinstance(e.args[0], list) else [str(e)]

    fires = central.list_rule_fires(detector_id=detector["id"], limit=500)
    by_outcome: dict[str, int] = {}
    for f in fires:
        by_outcome[f["outcome"]] = by_outcome.get(f["outcome"], 0) + 1
    hits = sum(by_outcome.get(o, 0) for o in _HIT_OUTCOMES)
    return {
        **detector,
        "condition_prose": prose,
        "condition_problems": problems,
        "readable": not problems,
        "remedies": [_remedy_entry(r) for r in central.remedy_rules_for(detector["id"])],
        "fires": {"total": len(fires), "by_outcome": by_outcome, "hits": hits},
        "hit_rate": (hits / len(fires)) if fires else None,
        "hit_rate_note": None if fires else (
            "this detector has never fired, so it has no hit rate yet — which is not a "
            "hit rate of zero"),
    }


def _remedy_entry(row: dict[str, Any]) -> dict[str, Any]:
    """A remedy row with its parameters DECODED, so no surface parses JSON itself"""
    return {**row, "params": db.from_json(row.get("params"), {})}


def rules_list(*, project: str = "", status: str = "",
               gap_class: str = "") -> dict[str, Any]:
    """The registry: what the OS has learned to recognise about itself"""
    central = CentralStore()
    try:
        every = central.list_detectors(project=project, gap_class=gap_class,
                                       include_retired=True)
        counts = {
            "total": len(every),
            "armed": sum(1 for d in every if d["status"] == "armed"),
            "dry_run": sum(1 for d in every if d["status"] == "dry_run"),
            "retracted": sum(1 for d in every if d["status"] == "retracted"),
        }
        shown = [d for d in every if not status or d["status"] == status]
        if not status:
            shown = [d for d in shown if d["retired_at"] is None]
        entries = [_rule_entry(central, d) for d in shown]
    finally:
        central.close()
    enabled = rules_enabled(project or None)
    if not counts["total"]:
        note = ("no detectors are registered — the registry is empty, which is a "
                "different thing from a registry whose rules have never matched")
    elif not enabled:
        # THE SENTENCE, NOT AN EMPTY COUNT
        note = (f"the evaluation pass is OFF ({counts['total']} rules registered and "
                f"nothing is evaluating them) — no rule here has been given the chance "
                f"to match. Turn it on with `jarvis config set <project> rules.enabled "
                f"true`")
    else:
        note = ("every rule is in dry run: it records what it would have proposed and "
                "acts on nothing. Only a person arms one")
        if counts["armed"]:
            note = (f"{counts['armed']} armed, the rest in dry run. An armed rule "
                    f"proposes; the gate still decides whether anything runs")
    return {"counts": counts, "rules": entries, "enabled": enabled, "note": note}


def rules_show(detector_id: str) -> dict[str, Any]:
    """One detector in full: the condition as prose"""
    central = CentralStore()
    try:
        detector = central.get_detector(detector_id)
        if detector is None:
            raise OpsError(f"detector {detector_id!r} not found — "
                           f"`jarvis rules list` shows the registered ones")
        entry = _rule_entry(central, detector)
        retired = [_remedy_entry(r) for r
                   in central.remedy_rules_for(detector_id, include_retired=True)
                   if r["retired_at"] is not None]
        fires = central.list_rule_fires(detector_id=detector_id, limit=20)
        # The reader §8's verdict owes (Neo question 974): a recurrence is the record that THIS rule did not...
        recurrences = central.list_recurrences(detector_id=detector_id, limit=20)
    finally:
        central.close()
    return {
        "detector": detector,
        "condition_prose": entry["condition_prose"],
        "condition_problems": entry["condition_problems"],
        "readable": entry["readable"],
        "remedies": entry["remedies"],
        "retired_remedies": retired,
        "provenance": {
            "source": detector["source"], "io_id": detector["io_id"],
            "fix_wo_id": detector["fix_wo_id"], "issue_url": detector["issue_url"],
            "pr_url": detector["pr_url"], "seed_version": detector["seed_version"],
        },
        "fires": fires,
        "recurrences": recurrences,
        "hit_rate": entry["hit_rate"],
        "hit_rate_note": entry["hit_rate_note"],
    }


def rules_retract(rule_id: str, reason: str) -> dict[str, Any]:
    """Retire a detector or one remedy rule"""
    if not reason.strip():
        raise OpsError("a retraction needs a reason — it is the only record of why the "
                       "OS stopped believing something it acted on")
    central = CentralStore()
    try:
        try:
            if rule_id.startswith("dt-"):
                rule = central.retract_detector(rule_id, reason.strip())
                note = ("retracted, with its live remedy rules — they no longer apply, "
                        "and the record keeps that they once did")
            elif rule_id.startswith("rm-"):
                rule = central.retract_remedy_rule(rule_id, reason.strip())
                note = ("retracted — the detector still recognises the gap, it just no "
                        "longer proposes this")
            else:
                raise OpsError(f"{rule_id!r} is neither a detector (`dt-…`) nor a "
                               f"remedy rule (`rm-…`)")
        except KeyError as e:
            raise OpsError(str(e)) from e
        except ValueError as e:
            raise OpsError(str(e)) from e
    finally:
        central.close()
    return {"rule": rule, "note": note}


def rules_arm(detector_id: str, reason: str, *, by: str = "user") -> dict[str, Any]:
    """Let a detector's fires act: the one place a person gives a rule authority (docs/superpowers/specs/2026-09-27-self-evolution.md)"""
    if not (reason or "").strip():
        raise OpsError("arming a rule needs a reason — it is the only record of why the "
                       "OS let this rule act")
    central = CentralStore()
    try:
        try:
            detector = central.arm_detector(detector_id, by=by, reason=reason.strip())
        except (KeyError, ValueError) as e:
            raise OpsError(str(e)) from e
        remedies_armed = central.remedy_rules_for(detector_id)
        central.add_inbox(
            project=detector["project"], level="info",
            title=f"{detector_id} [{detector['gap_class']}] is now ARMED by {by}",
            body=(f"{reason.strip()}\n\nIts fires now raise an alarm and ask the existing "
                  f"self-heal gate for permission; nothing runs without a grant. "
                  f"`jarvis rules false-positive <fire-id> --reason …` calls a fire wrong."))
    finally:
        central.close()
    return {"detector": detector, "remedies": [_remedy_entry(r) for r in remedies_armed],
            "note": ("armed — its fires raise an alarm and ask the self-heal gate; the "
                     "gate still decides whether anything runs")}


def rules_false_positive(fire_id: int, reason: str) -> dict[str, Any]:
    """A person says a fire was wrong — and, at the threshold, the rule goes back to dry run"""
    from .catalog import RulesConfig

    if not (reason or "").strip():
        raise OpsError("a false positive needs a reason — it is the evidence a later arm "
                       "is judged on")
    central = CentralStore()
    try:
        try:
            fire = central.record_false_positive(int(fire_id), reason.strip())
        except (KeyError, ValueError) as e:
            raise OpsError(str(e)) from e
        detector = central.get_detector(fire["detector_id"])
        cfg = rules_config(fire["project"] or None)
        limit = (cfg if cfg is not None else RulesConfig()).false_positive_disarm
        demoted = False
        if (detector is not None and detector["status"] == "armed"
                and int(detector["false_positives"]) >= limit):
            why = (f"{detector['false_positives']} fires marked false positives "
                   f"(limit {limit}); latest: fire {fire['id']} on {fire['order_id']} — "
                   f"{reason.strip()}")
            detector = central.disarm_detector(detector["id"], why)
            demoted = True
            central.add_inbox(
                project=fire["project"] or detector["project"], level="warning",
                title=(f"{detector['id']} [{detector['gap_class']}] returned to dry run: "
                       f"{detector['false_positives']} false positives"),
                body=why, wo_id=fire["order_id"] or None)
    finally:
        central.close()
    return {"fire": fire, "detector": detector, "demoted": demoted,
            "limit": limit,
            "note": (f"the detector is back in dry run — {why}" if demoted else
                     f"{detector['false_positives'] if detector else 0} of {limit} "
                     f"false positives before an armed rule returns to dry run")}


def rules_config(project: str | None = None) -> Any:
    """The `rules` settings in force for `project` — or the OS's — or None"""
    try:
        catalog = resolve_catalog()
        if project is None:
            return catalog.os.rules
        return catalog.project(project).rules
    except (OpsError, CatalogError, OSError, ValueError):
        return None


def rules_enabled(project: str | None = None) -> bool:
    """Is the evaluation pass on — for `project`, or fleet-wide?"""
    cfg = rules_config(project)
    return bool(cfg is not None and cfg.enabled)


def rule_facts(store: ProjectStore, wo: dict[str, Any], *, now: float,
               sources: frozenset[str] | set[str] | None = None,
               project: str = "", max_rounds: int | None = None) -> Any:
    """One work order's `rules.Facts`"""
    from . import automerge
    from . import budget as budget_mod
    from . import holds as holds_mod
    from . import invariants as invariants_mod
    from . import rules as rules_mod

    wanted = None if sources is None else frozenset(sources)

    def need(slug: str) -> bool:
        return wanted is None or slug in wanted

    wo_id = str(wo["id"])
    values: dict[str, Any] = {}
    # READ ONCE PER ORDER AND SHARED
    hold_rows: list[Any] | None = None

    def put(name: str, value: Any) -> None:
        """Record a field, or leave it ABSENT"""
        if value is None or value == "":
            return
        values[name] = value

    if need("work_order"):
        put("status", str(wo.get("status") or ""))
        put("kind", str(wo.get("kind") or ""))
        # The two flags are recorded as BOOLEANS even when false: a work order always has an answer to "is it...
        values["hidden"] = bool(wo.get("hidden"))
        values["needs_attention"] = bool(wo.get("needs_attention"))
        put("attention_reason", str(wo.get("attention_reason") or ""))

    if need("state_durations"):
        durations = state_durations(store, wo_id=wo_id, now=now)
        hold_rows = list(durations.holds)
        if durations.current_status_since is not None:
            since = durations.current_status_since
            put("seconds_in_status", max(0.0, now - since))
            # THE ACTIVE BASIS, spec §6 of docs/superpowers/specs/2026-09-30-time-in-...
            held = sum(h.overlap(since, now, now) for h in durations.holds)
            put("seconds_in_status_active", max(0.0, now - since - held))
        if durations.last_activity_ts is not None:
            put("seconds_since_activity", max(0.0, now - durations.last_activity_ts))
        if durations.spans:
            put("lifetime_seconds", max(0.0, now - durations.spans[0].entered))
        put("last_activity_kind", durations.last_activity_kind)

    if need("holds"):
        # THE EXPENSIVE ONE
        if hold_rows is None:
            hold_rows = list(holds_mod.held(store, wo_id, now=now))
        open_holds = [h for h in hold_rows if h.open]
        if open_holds:
            hold = open_holds[-1]
            put("hold_cause", hold.cause)
            put("hold_seconds", max(0.0, now - hold.started))

    if need("automerge"):
        state = automerge_state(store, wo)
        # A STALE HOLD IS NOT A CLAIM ABOUT NOW
        if automerge.hold_is_live(state):
            put("automerge_code", str((state or {}).get("code") or ""))

    if need("waiting_on"):
        put("waiting_on", str(waiting_on(store, wo, now=now).get("what") or ""))

    if need("validation_round"):
        latest = store.latest_validation_round(wo_id=wo_id)
        if latest is not None:
            round_no = int(latest["round"] or 0)
            put("round_no", round_no)
            put("round_outcome", str(latest["outcome"] or ""))
            put("judged_head_sha", str(latest["head_sha"] or ""))
            cap = max_rounds
            if cap is None:
                cfg = validation_config(project or None)
                cap = getattr(cfg, "max_rounds", None)
            # ABSENT rather than a guess when the catalog cannot answer: `rounds_left` is a subtraction from a...
            if cap is not None:
                put("rounds_left", max(0, int(cap) - round_no))

    if need("pull_request"):
        # THE RECORDED COLUMNS, never a live `gh` call: this runs for every open order on every sweep
        put("pr_url", str(wo.get("pr_url") or ""))
        put("pr_state", str(wo.get("pr_state") or ""))

    if need("event_counts"):
        values["event_counts"] = store.event_kind_counts(wo_id)

    if need("invariant_events"):
        # THE ROUTE A DETECTOR TAKES WHEN AN INVARIANT ALREADY DETECTS THE CONDITION
        counts: dict[str, int] = {}
        for event in store.events_of_kind(wo_id, "invariant"):
            name = str(db.from_json(event["payload"], {}).get("invariant") or "")
            if name:
                counts[name] = counts.get(name, 0) + 1
        values["invariant_events"] = counts

    if need("validation_failures"):
        # BY CAUSE, off the payloads — `event_counts` keys on the KIND
        causes: dict[str, int] = {}
        for event in store.events_of_kind(wo_id, "validation_failed"):
            cause = str(db.from_json(event["payload"], {}).get("cause") or "")
            if cause:
                causes[cause] = causes.get(cause, 0) + 1
        values["validation_failure_causes"] = causes

    if need("dependencies"):
        values["depends_on_count"] = len(store.dependencies(wo))
        values["dead_dependency_count"] = len(
            invariants_mod.dead_dependencies(store, wo))

    if need("neo_question"):
        question = invariants_mod.awaiting_neo(wo_id)
        if question is not None:
            put("neo_question_status", str(question.get("status") or ""))
            values["neo_question_attempts"] = int(question.get("attempts") or 0)

    if need("budget"):
        # NO BUDGET IS NOT A BUDGET OF ZERO
        if wo.get("budget_usd") is not None:
            values["budget_usd"] = float(wo["budget_usd"])
        values["spent_usd"] = budget_mod.spent(store, None, wo_id).total_usd

    return rules_mod.Facts(
        project=project, order_id=wo_id,
        # THE SUBJECT KIND, not `work_orders.kind`
        order_kind=rules_mod.DEFAULT_SUBJECT,
        values=values, now=now)


def rules_dry_run(detector_id: str, order_id: str = "") -> dict[str, Any]:
    """What this detector reads, and — given an order — what it would decide"""
    from . import rules as rules_mod

    central = CentralStore()
    try:
        detector = central.get_detector(detector_id)
        if detector is None:
            raise OpsError(f"detector {detector_id!r} not found")
        remedies_rows = [_remedy_entry(r)
                         for r in central.remedy_rules_for(detector_id)]
    finally:
        central.close()

    out: dict[str, Any] = {
        "detector": detector, "remedies": remedies_rows, "order_id": order_id,
        "readable": True, "evaluated": False, "matched": None, "problems": [],
        "fields": [], "sources": [], "condition_prose": None, "explanation": None,
        "absent": [], "note": "",
    }
    try:
        cond = rules_mod.parse_condition(detector["condition"])
    except rules_mod.RulesError as e:
        problems = list(e.args[0]) if e.args and isinstance(e.args[0], list) else [str(e)]
        out.update(readable=False, problems=problems, note=(
            "this condition could not be read, so nothing was evaluated and nothing was "
            "decided — a row written by an older release may name a field this one no "
            "longer has"))
        return out

    out.update(condition_prose=rules_mod.render_condition(cond),
               fields=sorted(rules_mod.fields_used(cond)),
               sources=sorted(rules_mod.sources_used(cond)))
    if not order_id:
        out["note"] = ("no order given, so nothing was evaluated — this is what the "
                       "condition reads")
        return out

    project, path, wo = find_work_order(order_id)
    store = ProjectStore(path)
    try:
        try:
            # LAZY HERE TOO, for the sweep's reason: only the sources this one condition names are read
            facts = rules_mod.facts(store, wo, now=db.now(),
                                    sources=rules_mod.sources_used(cond))
        except Exception as e:  # noqa: BLE001
            # A SNAPSHOT THAT COULD NOT BE BUILT DECIDES NOTHING
            out["note"] = (f"the fact snapshot for {order_id} could not be built, so "
                           f"nothing was evaluated and nothing was decided: {e}")
            return out
    finally:
        store.close()

    evaluation = rules_mod.evaluate(cond, facts)
    out.update(evaluated=True, matched=evaluation.matched,
               absent=list(evaluation.absent),
               explanation=rules_mod.explain(cond, evaluation),
               project=project,
               note="a dry run: this wrote nothing and acted on nothing")
    return out


# -- the evolution report (§9)

#: Generous caps on the three ledgers
_EVOLUTION_FIRE_LIMIT = 20_000
_EVOLUTION_RECURRENCE_LIMIT = 5_000
_EVOLUTION_KNOWLEDGE_LIMIT = 2_000
_EVOLUTION_INVESTIGATION_LIMIT = 2_000

#: The sentence a scope with NO EVER-ARMED RULE ships INSTEAD of a number
_NO_MECHANICAL_SHARE_NOTE = (
    "no rule has been armed yet, so there is no mechanical share to report — which is "
    "not a share of zero")

#: The OTHER absence, and a scope with an armed rule must never be told the sentence above: a rule IS...
_NOTHING_STUCK_SHARE_NOTE = (
    "nothing got stuck in this window, so there is nothing a rule could have resolved "
    "mechanically — which is not a share of zero")

#: `rules.effective_status`' precedence, as a lookup this module can sort by
_EVOLUTION_STATUS_STRENGTH = {rules.RETRACTED: 0, rules.DRY_RUN: 1, rules.ARMED: 2}


def _evolution_min_samples(project: str | None) -> int:
    """The RESOLVED `evolution.min_samples` for this scope (Neo q1575)"""
    from .catalog import DEFAULT_EVOLUTION_MIN_SAMPLES

    try:
        resolved = resolve_catalog()
    except (OpsError, CatalogError, OSError, ValueError):
        return DEFAULT_EVOLUTION_MIN_SAMPLES
    cfg = resolved.os.evolution
    if project:
        try:
            cfg = resolved.project(project).evolution
        except CatalogError:
            cfg = resolved.os.evolution
    return max(1, int(getattr(cfg, "min_samples", DEFAULT_EVOLUTION_MIN_SAMPLES)))


def _evolution_zone(project: str | None) -> tuple[str, Any, str | None]:
    """The timezone the week boundary is resolved in: `(name, tzinfo, note)`"""
    from datetime import timezone
    from zoneinfo import ZoneInfo

    from . import fleetcost
    from .catalog import DEFAULT_COST_WEEK_RESET_ZONE

    try:
        name = fleetcost.cost_config(project or None).week_reset_zone
    except Exception:  # noqa: BLE001 - a report is a read; see `_evolution_min_samples`
        name = DEFAULT_COST_WEEK_RESET_ZONE
    try:
        return name, ZoneInfo(name), None
    except Exception:  # noqa: BLE001 - missing tzdata
        return name, timezone.utc, (
            f"the configured week-reset zone {name!r} could not be loaded on this host, "
            f"so these weeks are bucketed in UTC and may not line up with the cost week")


def _evolution_week(ts: float, zone: Any) -> str:
    """The ISO date of the Monday of the LOCAL week containing `ts`, `YYYY-MM-DD`"""
    from datetime import datetime, timedelta

    local = datetime.fromtimestamp(float(ts), zone)
    return (local.date() - timedelta(days=local.weekday())).isoformat()


def _evolution_median(values: Sequence[float]) -> float | None:
    """The median, or `None` on no samples"""
    xs = sorted(float(v) for v in values)
    if not xs:
        return None
    mid = len(xs) // 2
    return xs[mid] if len(xs) % 2 else (xs[mid - 1] + xs[mid]) / 2.0


def _evolution_links(**pairs: Any) -> dict[str, str]:
    """A timeline event's links, with every ABSENT one left out of the dict entirely"""
    return {k: str(v) for k, v in pairs.items() if v}


def _evolution_investigations(scope: str, since: float, until: float,
                              unreadable: list[str]) -> dict[str, list[float]]:
    """`{order_id: [created_at, …]}` for every investigation opened in the window"""
    out: dict[str, list[float]] = {}
    try:
        paths = registered_project_paths()
    except Exception:  # noqa: BLE001 - the project registry itself is unreadable
        return out
    for name in ([scope] if scope else sorted(paths)):
        path = paths.get(name)
        if path is None:
            continue
        try:
            store = ProjectStore(path)
        except Exception:  # noqa: BLE001
            unreadable.append(name)
            continue
        try:
            for row in store.list_feature_orders(
                    statuses=None, kind="investigation",
                    limit=_EVOLUTION_INVESTIGATION_LIMIT):
                created = float(row.get("created_at") or 0.0)
                if not since <= created <= until:
                    continue
                metadata = db.from_json(row.get("metadata"), {}) or {}
                subject = str(metadata.get(SUBJECT_KEY) or "")
                if subject:
                    out.setdefault(subject, []).append(created)
        except Exception:  # noqa: BLE001
            unreadable.append(name)
        finally:
            store.close()
    return out


def _evolution_knowledge(central: CentralStore, scope: str) -> list[dict[str, Any]]:
    """Every knowledge row in scope, retracted ones included — §9 reports retractions"""
    return central.search_knowledge("", limit=_EVOLUTION_KNOWLEDGE_LIMIT,
                                    project=scope or None)


def evolution_report(project: str | None = None, *, days: int = 90) -> dict:
    """Is the OS getting better at running itself — and where is it still weak? §9"""
    scope = (project or "").strip()
    # §9: a report for a name that does not exist would read as a project that learned nothing
    known = registered_project_paths()
    if scope and scope not in known:
        raise OpsError(f"project {scope!r} not registered (known: {sorted(known)})")
    until = db.now()
    since = until - float(days) * 86400.0
    min_samples = _evolution_min_samples(scope or None)
    zone_name, zone, zone_note = _evolution_zone(scope or None)
    unreadable: dict[str, list[str]] = {"projects": [], "detectors": [], "fires": []}

    central = CentralStore()
    try:
        # `include_retired=True`: §9 reports RETRACTIONS
        detectors = central.list_detectors(project=scope, include_retired=True)
        all_fires = central.list_rule_fires(project=scope, limit=_EVOLUTION_FIRE_LIMIT)
        recurrence_rows = [
            r for r in central.list_recurrences(project=scope,
                                                limit=_EVOLUTION_RECURRENCE_LIMIT)
            if since <= float(r["ts"]) <= until]
        entries = [_rule_entry(central, d) for d in detectors]
        knowledge = _evolution_knowledge(central, scope)
        # Exemptions only: §9's claim is about what the OS LEARNED is not privileged
        gate_exemptions = central.gate_rules(role="exempt", include_retired=True)
    finally:
        central.close()

    fires = [f for f in all_fires if since <= float(f["ts"]) <= until]
    gap_of: dict[str, str] = {str(d["id"]): str(d["gap_class"]) for d in detectors}

    # -- the mechanical share
    resolved_weeks: dict[str, set[str]] = {}
    for f in fires:
        detector_id = str(f["detector_id"])
        if detector_id not in gap_of:
            # House rule 2: a fire whose detector cannot be resolved is NAMED
            unreadable["fires"].append(str(f["id"]))
            continue
        if f["outcome"] == rules.APPLIED and f["cleared_at"] is not None:
            resolved_weeks.setdefault(str(f["order_id"]), set()).add(
                _evolution_week(f["ts"], zone))
    investigated = _evolution_investigations(scope, since, until,
                                             unreadable["projects"])
    # UNION, so an order that both resolved itself and was investigated is one order
    denominator_orders = set(resolved_weeks) | set(investigated)
    numerator = len(resolved_weeks)
    denominator = len(denominator_orders)

    # ABSENCE IS DECIDED BY WHETHER A RULE WAS EVER ARMED
    ever_armed = any(d["armed_at"] is not None or str(d["status"]) == rules.ARMED
                     for d in detectors) or any(
        str(f["mode"]) == rules.ARMED for f in all_fires)

    # One point per week WITH DATA
    week_resolved: dict[str, set[str]] = {}
    week_investigated: dict[str, set[str]] = {}
    for order_id, weeks in resolved_weeks.items():
        for week in weeks:
            week_resolved.setdefault(week, set()).add(order_id)
    for order_id, created_list in investigated.items():
        for created in created_list:
            week_investigated.setdefault(_evolution_week(created, zone), set()).add(
                order_id)
    series = []
    # The same rule one level down (§9): a week with investigations and no armed rule contributes no...
    for week in (sorted(set(week_resolved) | set(week_investigated))
                 if ever_armed else []):
        got = week_resolved.get(week, set())
        both = got | week_investigated.get(week, set())
        series.append({"week": week, "numerator": len(got), "denominator": len(both),
                       "value": (len(got) / len(both)) if both else None})

    value = (numerator / denominator) if (ever_armed and denominator) else None
    mechanical_share = {
        "value": value,
        # TWO reasons a share is absent, and each gets its own sentence: nothing ever armed
        "note": None if value is not None else (
            _NOTHING_STUCK_SHARE_NOTE if ever_armed else _NO_MECHANICAL_SHARE_NOTE),
        "numerator": numerator, "denominator": denominator, "series": series,
        "unreadable_projects": unreadable["projects"],
    }

    # -- by gap class
    buckets: dict[str, dict[str, int]] = {}
    for f in fires:
        gap_class = gap_of.get(str(f["detector_id"]))
        if gap_class is None:
            continue
        week = _evolution_week(f["ts"], zone)
        weeks_of = buckets.setdefault(gap_class, {})
        weeks_of[week] = weeks_of.get(week, 0) + 1
    by_gap_class = [
        {"gap_class": gap_class,
         "weeks": [{"week": w, "count": n} for w, n in sorted(weeks.items())],
         "total": sum(weeks.values())}
        for gap_class, weeks in sorted(buckets.items())]

    # -- per rule
    fires_by_detector: dict[str, list[dict[str, Any]]] = {}
    for f in fires:
        fires_by_detector.setdefault(str(f["detector_id"]), []).append(f)
    per_rule = []
    for detector, entry in zip(detectors, entries, strict=True):
        if not entry["readable"]:
            # The condition would not parse
            unreadable["detectors"].append(str(detector["id"]))
        # The weaker of detector and remedy, through `rules.effective_status`, which owns that precedence
        pairs = [rules.effective_status(detector, r) for r in entry["remedies"]]
        if pairs:
            status = max(pairs, key=lambda s: _EVOLUTION_STATUS_STRENGTH.get(s, 0))
        else:
            status = (rules.RETRACTED if detector["retired_at"]
                      else str(detector["status"]))
        cleared = [f["cleared_seconds"]
                   for f in fires_by_detector.get(str(detector["id"]), [])
                   if f["cleared_seconds"] is not None]
        enough = len(cleared) >= min_samples
        per_rule.append({
            "id": detector["id"], "gap_class": detector["gap_class"],
            "summary": detector["summary"], "status": status,
            "hits": detector["hits"], "false_positives": detector["false_positives"],
            "recurrences": detector["recurrences"],
            "last_fired": detector["last_fired"],
            "last_cleared": detector["last_cleared"],
            # Straight off `_rule_entry` rather than recomputed: the registry listing and this report must not be...
            "hit_rate": entry["hit_rate"], "hit_rate_note": entry["hit_rate_note"],
            "readable": entry["readable"],
            "condition_problems": entry["condition_problems"],
            "median_cleared_seconds": _evolution_median(cleared) if enough else None,
            "median_cleared_seconds_note": None if enough else (
                f"{len(cleared)} cleared fires in this window, fewer than the "
                f"{min_samples} this median needs — not a median yet"),
        })

    # -- recurrences
    verdict_counts = {v: 0 for v in rules.RECURRENCE_VERDICTS}
    for r in recurrence_rows:
        verdict = str(r["verdict"])
        verdict_counts[verdict] = verdict_counts.get(verdict, 0) + 1
    missed = verdict_counts.get(rules.MISSED, 0)
    remedy_failed = verdict_counts.get(rules.REMEDY_FAILED, 0)
    # kn-289d4b89: `unreadable` and `not_armed` are COUNTED and REPORTED and never folded into either side
    excluded = {rules.UNREADABLE_RECURRENCE:
                verdict_counts.get(rules.UNREADABLE_RECURRENCE, 0),
                rules.NOT_ARMED: verdict_counts.get(rules.NOT_ARMED, 0)}
    if missed == remedy_failed == 0:
        weaker, half_note = None, (
            "no recurrence has been recorded in this window that was either missed or "
            "whose remedy failed, so neither conditions nor remedies can be called the "
            "weaker half — which is not the two of them being equally good")
    # §9: the noun agrees with the count the sentence already holds
    elif missed == remedy_failed:
        weaker, half_note = None, (
            f"conditions and remedies have each failed {missed} "
            f"{'time' if missed == 1 else 'times'} in this window — "
            f"equally often, which is not a tie to be broken: both halves want work")
    elif missed > remedy_failed:
        weaker, half_note = "conditions", (
            f"conditions are the weaker half: {missed} "
            f"{'recurrence' if missed == 1 else 'recurrences'} the condition missed "
            f"against {remedy_failed} where the remedy did not hold")
    else:
        weaker, half_note = "remedies", (
            f"remedies are the weaker half: {remedy_failed} "
            f"{'recurrence' if remedy_failed == 1 else 'recurrences'} where the remedy "
            f"did not hold against {missed} the condition missed")
    recurrences = {
        "rows": recurrence_rows, "verdict_counts": verdict_counts,
        "weaker_half": {
            "weaker": weaker, "missed": missed, "remedy_failed": remedy_failed,
            "note": half_note, "excluded": excluded,
            "excluded_note": (
                "`unreadable` and `not_armed` recurrences are counted here and kept out "
                "of that comparison: neither is evidence about a condition or a remedy"),
        },
    }

    # -- stuck resolution
    samples: dict[str, dict[str, list[float]]] = {}
    for f in fires:
        gap_class = gap_of.get(str(f["detector_id"]))
        if gap_class is None or f["cleared_seconds"] is None:
            continue
        side = str(f["mode"])
        samples.setdefault(gap_class, {rules.DRY_RUN: [], rules.ARMED: []}).setdefault(
            side, []).append(float(f["cleared_seconds"]))
    stuck_resolution = []
    for gap_class, sides in sorted(samples.items()):
        dry = sides.get(rules.DRY_RUN, [])
        armed = sides.get(rules.ARMED, [])
        if len(dry) < min_samples or len(armed) < min_samples:
            stuck_resolution.append({
                "gap_class": gap_class, "dry_run_median": None, "armed_median": None,
                "delta": None,
                "note": (f"{len(dry)} dry-run and {len(armed)} armed samples, fewer "
                         f"than the {min_samples} this median needs — not enough "
                         f"history to compare yet")})
            continue
        dry_median = _evolution_median(dry)
        armed_median = _evolution_median(armed)
        stuck_resolution.append({
            "gap_class": gap_class, "dry_run_median": dry_median,
            "armed_median": armed_median,
            # Negative is the improvement: armed clears faster than dry run
            "delta": (armed_median or 0.0) - (dry_median or 0.0), "note": None,
            "dry_run_samples": len(dry), "armed_samples": len(armed)})

    # -- the timeline
    timeline: list[dict[str, Any]] = []

    def _add(ts: Any, kind: str, ident: Any, headline: str,
             links: dict[str, str] | None = None) -> None:
        if ts is None or not since <= float(ts) <= until:
            return
        timeline.append({"ts": float(ts), "kind": kind, "id": str(ident),
                         "headline": headline, "links": links or {}})

    for d in detectors:
        links = _evolution_links(io=d["io_id"], fix_wo=d["fix_wo_id"],
                                 issue=d["issue_url"], pr=d["pr_url"])
        _add(d["ts"], "detector_added", d["id"],
             f"a detector for {d['gap_class']} was registered"
             + (f": {d['summary']}" if d["summary"] else ""), links)
        _add(d["armed_at"], "detector_armed", d["id"],
             f"{d['gap_class']} was armed"
             + (f" by {d['armed_by']}" if d["armed_by"] else "")
             + (f": {d['armed_reason']}" if d["armed_reason"] else ""), links)
        _add(d["retired_at"], "detector_retracted", d["id"],
             f"the {d['gap_class']} detector was retracted"
             + (f": {d['retired_reason']}" if d["retired_reason"] else ""), links)

    for k in knowledge:
        # `fix_wo` names the work order responsible
        _add(k["ts"], "knowledge_added", k["id"],
             f"learned: {str(k['content'])[:120]}",
             _evolution_links(fix_wo=k.get("wo_id")))
        _add(k["retired_at"], "knowledge_retracted", k["id"],
             f"retracted: {str(k.get('retired_reason') or '')[:120]}",
             _evolution_links(fix_wo=k.get("retired_by_wo_id")))

    for g in gate_exemptions:
        _add(g["ts"], "gate_exemption_learned", g["id"],
             f"learned that {g['pattern']!r} is not a privileged action"
             + (f": {g['reason']}" if g["reason"] else ""),
             _evolution_links(fix_wo=g.get("wo_id")))
        _add(g["retired_at"], "gate_exemption_retracted", g["id"],
             f"the exemption for {g['pattern']!r} was retracted"
             + (f": {g['retired_reason']}" if g["retired_reason"] else ""),
             _evolution_links(fix_wo=g.get("wo_id")))

    # A gap class FIRST SEEN, from the OS's OWN records — the earliest detector row or fire for that class
    first_seen: dict[str, float] = {}
    for d in detectors:
        gap_class = str(d["gap_class"])
        ts = float(d["ts"])
        first_seen[gap_class] = min(first_seen.get(gap_class, ts), ts)
    for f in all_fires:
        gap_class = gap_of.get(str(f["detector_id"]))
        if gap_class is None:
            continue
        ts = float(f["ts"])
        first_seen[gap_class] = min(first_seen.get(gap_class, ts), ts)
    for gap_class, ts in first_seen.items():
        _add(ts, "gap_class_first_seen", gap_class,
             f"the OS recognised {gap_class} for the first time")

    timeline.sort(key=lambda e: e["ts"], reverse=True)

    return {
        "window": {"days": int(days), "since": since, "until": until,
                   "week_zone": zone_name, "week_zone_note": zone_note},
        "project": scope or None,
        "min_samples": min_samples,
        "mechanical_share": mechanical_share,
        "by_gap_class": by_gap_class,
        "per_rule": per_rule,
        "recurrences": recurrences,
        "stuck_resolution": stuck_resolution,
        "timeline": timeline,
        "unreadable": unreadable,
    }


# -- the recurrence ledger (spec §8)

#: The sentence a person reads for each verdict
_RECURRENCE_NOTES = {
    rules.NOT_ARMED: (
        "a detector for this gap exists but is still in dry_run, so it acted on nothing "
        "— neither its condition nor its remedy can be blamed, and this recurrence is "
        "evidence FOR arming it"),
    rules.MISSED: (
        "the detector is armed and has no usable fire on this order, so the condition "
        "did not match what actually happened — the finding is about the CONDITION"),
    rules.REMEDY_FAILED: (
        "the detector is armed and did fire on this order, and the gap recurred anyway "
        "— the finding is about the REMEDY, or about the gate that refused it"),
    rules.UNREADABLE_RECURRENCE: (
        "the detector is armed and its newest fire on this order says something could "
        "not be READ, so nothing was decided — the finding is about what the condition "
        "reads, and this is NOT evidence for arming"),
}


def _recurrence_tracker_act(detector: dict[str, Any], *, order_id: str, gap_class: str,
                            verdict: str) -> str:
    """Reopen or comment on the ORIGINAL issue, and return the `filed_note`"""
    from . import bugreport, issues

    body = rules.recurrence_comment(order_id=order_id, gap_class=gap_class,
                                    verdict=verdict, detector_id=detector["id"])
    try:
        url = str(detector["issue_url"] or "")
        if not url:
            # Searched by the CAUSE — the detector id and the gap class
            found = issues.recurrence_issues(detector["id"], gap_class)
            if not found:
                return ("no original issue is recorded on the detector and none was "
                        "found on the tracker, searched by detector id and gap class "
                        "with `--state all`, so nothing was filed — the recurrence is "
                        "recorded here only")
            url = str(found[0]["url"])
        # Before `--add-label`: `gh` refuses a label the repository does not define
        issues.ensure_regression_label(bugreport.bug_repo())
        # `Issue.closed`, not `issues.CLOSED`: that constant is the DESIRED-state vocabulary of the...
        if issues.view(url).closed:
            issues.reopen(url, body)
            act = "reopened"
        else:
            issues.comment(url, body)
            act = "commented on"
        issues.add_label(url, issues.REGRESSION_LABEL)
        return f"{act} the original issue {url} and labelled it `regression`"
    except GitHubError as e:
        # `IssueLifecycleError` and `GhUnavailable` both derive from `GitHubError`
        return (f"the tracker was not updated: {e.reason} ({e}). The recurrence is "
                f"recorded here, and the ledger is the OS's own record — the tracker "
                f"mirrors it")


def record_recurrence(*, gap_class: str, project: str, order_id: str, io_id: str = "",
                      note: str = "", detector_id: str = "") -> dict[str, Any]:
    """An existing rule did not hold"""
    central = CentralStore()
    try:
        detector = (central.get_detector(detector_id) if detector_id else
                    rules.recurrence(
                        central.list_detectors(project=project, gap_class=gap_class),
                        gap_class, project=project))
        if detector is None:
            return {"recorded": False, "detector": None, "verdict": None,
                    "recurrence": None, "filed_note": "",
                    "verdict_note": (
                        f"no detector exists for gap class {gap_class!r}, so this is a "
                        f"NEW gap and not a recurrence — nothing was recorded against a "
                        f"rule, because there is no rule to blame")}
        detector = dict(detector)

        # The NEWEST fire this detector has on this order, CLEARED OR NOT
        candidates = [f for f in (
            central.open_rule_fire(detector["id"], order_id),
            *central.list_rule_fires(detector_id=detector["id"], order_id=order_id,
                                     limit=1)) if f]
        fire = (max(candidates, key=lambda f: (float(f["ts"]), int(f["id"])))
                if candidates else None)
        verdict = rules.recurrence_verdict(detector, fire)

        # Written BEFORE the tracker is touched
        row = central.add_recurrence(
            gap_class=gap_class, detector_id=detector["id"], project=project,
            order_id=order_id, verdict=verdict, io_id=io_id,
            original_fix_wo_id=detector["fix_wo_id"],
            original_issue_url=detector["issue_url"], filed_note="", note=note)
        filed_note = _recurrence_tracker_act(detector, order_id=order_id,
                                             gap_class=gap_class, verdict=verdict)
        row = central.set_recurrence_filed_note(row["id"], filed_note)
    finally:
        central.close()
    # `verdict_note` is the OS's sentence about the verdict
    return {"recorded": True, "detector": detector, "verdict": verdict,
            "recurrence": row, "filed_note": filed_note,
            "verdict_note": _RECURRENCE_NOTES[verdict]}


def explain_gate(command: str, project_name: str | None = None) -> dict[str, Any]:
    """Why this command would, or would not, trip a gate"""
    from .gate_rules import (
        KIND_NAMES,
        RuleSet,
        command_names,
        files_a_claim,
        gate_paperwork,
        list_segments,
        reads_only,
        scannable,
        shape_of,
    )

    if command.strip().isdigit():
        _, _, approval = _find_approval(int(command.strip()), project_name)
        command = approval["command"]
    # Without a project, every gate is treated as live: the question being asked is what the RULES say
    config = _project_gate_config(project_name) if project_name else None
    enabled = config.enabled if config else frozenset(KIND_NAMES)
    extra = config.extra_patterns if config else {}
    central = CentralStore()
    try:
        rules = RuleSet.load(central)
    finally:
        central.close()
    decision = rules.decide(command, enabled, extra)
    out: dict[str, Any] = {
        "command": command,
        "gates_enabled": sorted(enabled),
        "reads_only": reads_only(command),
        "gate_paperwork": gate_paperwork(command),
        # Per SEGMENT, so a filing reached through an interpreter or written in more than one command shows... (#233)
        "files_a_claim": sorted({command[s:e].strip().splitlines()[0]
                                 for s, e in list_segments(command)
                                 if command[s:e].strip() and files_a_claim(command[s:e])}),
        "commands_in_chain": sorted(command_names(command)),
        "scanned": scannable(command),
        "trace": list(decision.trace),
        "cleared_by": [{"rule": r, "kind": k, "pattern": p} for r, k, p in decision.cleared],
        "gated": decision.match is not None,
    }
    if decision.match:
        shape = shape_of(command, decision.match.pattern)
        out["gate"] = decision.match.kind
        out["matched"] = decision.match.pattern
        out["rule"] = decision.match.rule_id
        out["where"] = shape.describe() if shape else "unknown"
        out["learnable"] = bool(shape and shape.exemptible)
        out["why_unlearnable"] = (
            shape.unlearnable_reason() if shape
            else "the pattern does not occur in the command as written")
    return out


def show_gate(approval_id: int, project_name: str | None = None) -> dict[str, Any]:
    """One approval request in full, including the text the reviewer saw"""
    from .neo_store import NeoStore

    name, _path, approval = _find_approval(approval_id, project_name)
    question = None
    if approval["neo_question_id"]:
        neo = NeoStore()
        try:
            question = neo.get(approval["neo_question_id"])
        finally:
            neo.close()
    return {**approval, "project": name, "neo_question": question}


# -- the config console

# Which reads of a setting a change actually reaches (§4.2), by resolved path
APPLY_RULES: tuple[tuple[str, str], ...] = (
    ("os.validation.*", "hot"),
    ("os.neo.*", "hot"),
    ("os.ui.*", "restart"),
    ("projects.*.path", "restart"),
    ("projects.*.settings_overrides", "restart"),
    ("projects.*.settings_overrides.*", "restart"),
    ("*.model", "next-dispatch"),
    ("*.effort", "next-dispatch"),
    ("*.permission_mode", "next-dispatch"),
    # Read once per spawn into the worker's settings file (docs/superpowers/specs/2026-10-01-the-steer-that-beat-the-brief.md)
    ("*.bash_first", "next-dispatch"),
    # Read once per spawn into the worker's settings file (docs/superpowers/specs/2026-10-02-serena-the-cheap-path.md)
    ("*.tool_search", "next-dispatch"),
    # Read once per spawn into the worker's settings file (docs/superpowers/specs/2026-10-15-the-hour-on-worker-turns-only.md)
    ("*.cache_ttl", "next-dispatch"),
    # Read once per spawn into the worker's settings file, which is where the hook reads it (docs/superpowers/specs/2026-10-02-serena-the-cheap-path.md)
    ("*.py_nav_hook", "next-dispatch"),
    # Both read once per spawn into the worker's settings file, which is where the hook reads them (docs/superpowers/specs/2026-10-06-navigate-specs-like-code.md)
    ("*.doc_nav_hook", "next-dispatch"),
    ("*.doc_read_limit_lines", "next-dispatch"),
    ("*.autocompact_window", "next-dispatch"),
    ("*.append_system_prompt", "next-dispatch"),
    # Read once per spawn, into the settings file that spawn passes to `--settings`...
    ("*.wiring.*", "next-dispatch"),
    # Same reason as `wiring`, one layer down: the cap reaches the hook as `JARVIS_SUMMARY_MAX_WORDS`
    ("*.concision.*", "next-dispatch"),
    # `jarvis cost --fleet` reads the catalog on every invocation (docs/superpowers/specs/2026-10-06-fleet-cost-distribution.md)
    ("*.cost.*", "hot"),
    # Re-read per probe round in the UI process and per tick in the daemon (docs/superpowers/specs/2026-10-08-the-dashboard-reports-and-heals-its-own-wedge.md)
    ("*.ui_health.*", "hot"),
)

APPLY_NOTES = {
    "hot": "in force on the daemon's next tick",
    "next-dispatch": "applies to work orders dispatched from now on — a worker already "
                     "running keeps what it was dispatched with",
    "restart": "NOT in force until `jarvis start` restarts the OS",
}

# `settings_overrides` is `restart` for a reason the class name does not carry: nothing re-runs...
SETTINGS_OVERRIDES_NOTE = (
    "nothing re-runs bootstrap_project, so the project's .claude settings on disk are "
    "unchanged until `jarvis start` writes them"
)


def apply_class(path: str) -> str:
    """`hot`, `next-dispatch` or `restart` for one resolved path (§4.2)"""
    for glob, cls in APPLY_RULES:
        if fnmatch.fnmatch(path, glob):
            return cls
    return "hot"


def apply_note(path: str) -> str:
    cls = apply_class(path)
    if cls == "restart" and ".settings_overrides" in f".{path}.":
        return SETTINGS_OVERRIDES_NOTE
    return APPLY_NOTES[cls]


def safety_key(path: str) -> bool:
    """Does this path change what a worker is ALLOWED to do, rather than what it costs?"""
    return any(fnmatch.fnmatch(path, glob) for glob in SAFETY_KEYS)


def _refuse_worker_write(verb: str) -> None:
    """A worker may not change the fleet's own configuration (§7)"""
    wo_id = os.environ.get("JARVIS_WO_ID")
    if not wo_id:
        return
    raise OpsError(
        f"{wo_id} is a worker session — a worker may not `jarvis config {verb}`. "
        "The fleet's configuration is the user's. Ask on the work order record "
        "(`jarvis wo ask`), or file it: `jarvis backlog add jarvis_os \"…\"`."
    )


def _catalog_file(catalog_path: str | None = None) -> Path:
    """The file `jarvis config` rewrites: the one registered at `jarvis start`"""
    if catalog_path:
        return Path(catalog_path).expanduser().resolve()
    central = CentralStore()
    try:
        stored = central.get_state("catalog_path")
    finally:
        central.close()
    if not stored:
        raise OpsError(
            "no catalog is registered — run `jarvis start --catalog <file>` first, "
            "or pass --catalog <file>")
    return Path(stored)


def _read_document(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except OSError as e:
        raise OpsError(f"cannot read the catalog at {path}: {e}") from e
    except ValueError as e:
        raise OpsError(f"the catalog at {path} is not valid JSON: {e}") from e
    if not isinstance(data, dict):
        raise OpsError(f"the catalog at {path} must be a JSON object")
    return data


def _write_document(path: Path, document: dict[str, Any]) -> None:
    """Rewrite the catalog from the canonical document, atomically"""
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(config_version.canonicalise(document) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        raise OpsError(f"cannot write the catalog at {path}: {e}") from e


def _resolved_of(document: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    """Parse-and-flatten a document, raising `OpsError` rather than `CatalogError`"""
    try:
        cat = parse_catalog(json.loads(json.dumps(document)), source_path=path)
    except CatalogError as e:
        raise OpsError(str(e)) from None
    return config_version.resolve(cat)


def _commit_document(document: dict[str, Any], *, path: Path, actor: str,
                     reason: str, changes: list[dict[str, Any]]) -> dict[str, Any]:
    """Validate, rewrite the file, write the version row — in that order (§3)"""
    resolved = _resolved_of(document, path)
    _write_document(path, document)
    central = CentralStore()
    try:
        return central.add_config_version(
            document, resolved, actor=actor, reason=reason, changes=changes,
            source_path=str(path))
    finally:
        central.close()


def parse_config_value(text: str) -> Any:
    """The CLI hands values in as text"""
    try:
        return json.loads(text)
    except ValueError:
        return text


def _project_names(document: dict[str, Any]) -> list[str]:
    return [p["name"] for p in document.get("projects", [])
            if isinstance(p, dict) and isinstance(p.get("name"), str)]


#: The OS-owning project's own health sweep is not a setting the user may turn off
OS_SWEEP_KEYS = ("supervisor.enabled", "supervisor.health_enabled")


def _refuse_os_sweep_off(key: str, document: dict[str, Any],
                         resolved: dict[str, Any], file: Path) -> None:
    """Refuse a write that would leave the OS's own health sweep off"""
    from . import schedule

    suffix = next((k for k in OS_SWEEP_KEYS if key == k or key.endswith(f".{k}")), None)
    if suffix is None:
        return
    projects = [p for p in document.get("projects", [])
                if isinstance(p, dict) and isinstance(p.get("name"), str)]
    try:
        owner = schedule.os_project(
            (p["name"], Path(str(p.get("path") or file.parent))) for p in projects)
    except (OSError, ValueError):       # an unreadable path is not an authorisation
        return
    if owner is None:
        return
    # A write scoped to ANOTHER project cannot move the owner's resolved value
    if key.startswith("projects.") and not key.startswith(f"projects.{owner}."):
        return
    if not resolved.get(f"projects.{owner}.{suffix}"):
        raise OpsError(
            f"projects.{owner}.{suffix} would leave the health sweep of {owner} OFF, "
            f"and that project runs the OS itself — its sweep must always be on and "
            f"producing judgements (user rule, 2026-09-28). Change it on another "
            f"project, or turn the sweep off nowhere.")


def _key_path(path: str, project: str | None, document: dict[str, Any]) -> str:
    """The key-space path a user's `<path>` and optional `<project>` name together"""
    key = path.strip().strip(".")
    if not key:
        raise OpsError("give a setting path, e.g. `os.validation.enabled`")
    known = _project_names(document)

    if project is None:
        head = key.split(".")[0]
        if head not in ("os", "projects"):
            raise OpsError(
                f"{key!r} is not a setting path — give `os.…` or `projects.<name>.…`, "
                f"or name the project: `jarvis config set <project> {key} …`")
        if head == "projects":
            parts = key.split(".")
            if len(parts) < 3:
                raise OpsError(f"{key!r} names a whole project, not a setting in it")
            if parts[1] not in known:
                raise OpsError(f"unknown project {parts[1]!r} (known: {known})")
        return key

    if project not in known:
        raise OpsError(f"unknown project {project!r} (known: {known})")
    prefix = f"projects.{project}."
    if key.startswith(prefix):
        return key
    if key.split(".")[0] in ("os", "projects"):
        raise OpsError(
            f"{key!r} is not a path under project {project!r} — drop the project to "
            f"set it, or give a path relative to the project (`validation.enabled`)")
    return prefix + key


def _document_slot(document: dict[str, Any], key: str, *,
                   create: bool) -> tuple[dict[str, Any] | None, str]:
    """The container object and final key a key-space path names in the raw document"""
    parts = key.split(".")
    if parts[0] == "os":
        node = document.setdefault("os", {}) if create else document.get("os")
        rest, seen = parts[1:], "os"
    else:
        node = next((p for p in document.get("projects", [])
                     if isinstance(p, dict) and p.get("name") == parts[1]), None)
        rest, seen = parts[2:], f"projects.{parts[1]}"
    if not rest:
        raise OpsError(f"{key!r} names a whole section, not a setting")

    for seg in rest[:-1]:
        if node is None:
            return None, rest[-1]
        if not isinstance(node, dict):
            raise OpsError(f"{seen} is not an object in the catalog file")
        seen = f"{seen}.{seg}"
        if seg not in node and create:
            node[seg] = {}
        node = node.get(seg)
    if node is not None and not isinstance(node, dict):
        raise OpsError(f"{seen} is not an object in the catalog file")
    return node, rest[-1]


#: A write that moved the DOCUMENT and not the effective value
DOCUMENT_ONLY_KINDS = ("pinned", "unpinned")


def _retry(verb: str, path: str, project: str | None, value: str = "") -> str:
    """The exact command to re-run, spelled the way the user spelled it"""
    words = ["jarvis", "config", verb, *([project] if project else []), path]
    if value:
        words.append(value)
    return " ".join(words) + ' --reason "…"'


def _one_change(key: str, before: dict[str, Any], after: dict[str, Any],
                doc_before: Any, doc_after: Any, existed: bool,
                *, removed: bool = False) -> list[dict[str, Any]]:
    """The single triple a `set`/`unset` asked for, read off the RESOLVED maps"""
    for change in config_version.diff(before, after):
        if change["path"] == key:
            return [change]
    if key in before or key in after:
        return [{"path": key, "kind": "unpinned" if removed else "pinned",
                 "old": before.get(key), "new": after.get(key)}]
    return [{"path": key, "kind": "changed" if existed else "added",
             "old": doc_before, "new": doc_after}]


def _require_reason(changes: list[dict[str, Any]], reason: str,
                    retry: str = "") -> None:
    """A safety key demands a `--reason` only when its EFFECTIVE value moves (§7)"""
    unsafe = [c["path"] for c in changes if safety_key(c["path"])
              and c["kind"] not in DOCUMENT_ONLY_KINDS and c["old"] != c["new"]]
    if not unsafe or reason.strip():
        return
    more = f" (and {len(unsafe) - 3} more)" if len(unsafe) > 3 else ""
    raise OpsError(
        f"{', '.join(unsafe[:3])}{more} — a safety setting changes what a worker is "
        f"ALLOWED to do, so `--reason` is required and goes on the version row"
        + (f":\n    {retry}" if retry else ""))


def _find_version(version_id: str) -> dict[str, Any]:
    """A version by id, or by any unambiguous prefix of one"""
    central = CentralStore()
    try:
        row = central.get_config_version(version_id)
        if row is not None:
            return row
        hits = [v for v in central.config_versions(limit=1000)
                if v["id"].startswith(version_id)]
    finally:
        central.close()
    if len(hits) == 1:
        return hits[0]
    if not hits:
        raise OpsError(f"no config version {version_id!r} — `jarvis config history`")
    raise OpsError(f"{version_id!r} matches {len(hits)} versions: "
                   f"{[h['id'] for h in hits]}")


def _touches(paths: Sequence[str], project: str | None) -> bool:
    """Does a version's change list reach `project`'s effective configuration?"""
    if not project or not paths:
        return True
    return any(not p.startswith("projects.") or p.startswith(f"projects.{project}.")
               for p in paths)


def _in_scope(resolved: dict[str, Any], project: str | None) -> dict[str, Any]:
    """A project's own settings plus the fleet settings it runs under"""
    if not project:
        return resolved
    prefix = f"projects.{project}."
    return {k: v for k, v in resolved.items()
            if k.startswith(prefix) or k.startswith("os.")}


def set_config(path: str, value: Any, project: str | None = None, *, reason: str = "",
               catalog_path: str | None = None, actor: str = "user") -> dict[str, Any]:
    """Write one setting: validate, rewrite the catalog, record the version (§3)"""
    _refuse_worker_write("set")
    file = _catalog_file(catalog_path)
    document = _read_document(file)
    key = _key_path(path, project, document)

    try:
        before = _resolved_of(document, file)
    except OpsError:
        before = {}  # an already-invalid catalog is exactly what a `set` may be fixing
    was = config_version.version_id(document)
    container, leaf = _document_slot(document, key, create=True)
    assert container is not None  # `create=True` builds the whole chain
    existed, doc_before = leaf in container, container.get(leaf)
    container[leaf] = value

    after = _resolved_of(document, file)
    _refuse_os_sweep_off(key, document, after, file)
    changes = _one_change(key, before, after, doc_before, value, existed)
    _require_reason(changes, reason,
                    _retry("set", path, project, json.dumps(value, ensure_ascii=False)))
    row = _commit_document(document, path=file, actor=actor, reason=reason,
                           changes=changes)
    return {"version": row, "changed": row["id"] != was,
            "path": key, "value": value, "change": changes[0],
            "apply": apply_class(key), "note": apply_note(key),
            "safety": safety_key(key), "catalog": str(file)}


def unset_config(path: str, project: str | None = None, *, reason: str = "",
                 catalog_path: str | None = None,
                 actor: str = "user") -> dict[str, Any]:
    """Remove one setting from the document, so it falls back to its default"""
    _refuse_worker_write("unset")
    file = _catalog_file(catalog_path)
    document = _read_document(file)
    key = _key_path(path, project, document)

    before = _resolved_of(document, file)
    container, leaf = _document_slot(document, key, create=False)
    if container is None or leaf not in container:
        raise OpsError(
            f"{key} is not set in the catalog file — it is already running on its "
            f"default ({before.get(key)!r})")
    doc_before = container.pop(leaf)

    after = _resolved_of(document, file)
    _refuse_os_sweep_off(key, document, after, file)
    changes = _one_change(key, before, after, doc_before, after.get(key), True,
                          removed=True)
    _require_reason(changes, reason, _retry("unset", path, project))
    row = _commit_document(document, path=file, actor=actor, reason=reason,
                           changes=changes)
    return {"version": row, "changed": True, "path": key, "value": after.get(key),
            "change": changes[0], "apply": apply_class(key), "note": apply_note(key),
            "safety": safety_key(key), "catalog": str(file)}


def config_get(path: str, project: str | None = None,
               catalog_path: str | None = None) -> dict[str, Any]:
    """One setting as the fleet would read it, and whether the file says so"""
    file = _catalog_file(catalog_path)
    document = _read_document(file)
    key = _key_path(path, project, document)
    resolved = _resolved_of(document, file)
    if key not in resolved:
        raise OpsError(
            f"{key} is not a known setting — `jarvis config show` lists every path")
    container, leaf = _document_slot(document, key, create=False)
    written = container is not None and leaf in container
    return {"path": key, "value": resolved[key], "written": written,
            "apply": apply_class(key), "note": apply_note(key),
            "safety": safety_key(key), "catalog": str(file)}


def _written_paths(document: dict[str, Any], resolved: dict[str, Any]) -> list[str]:
    """Which of the resolved paths the DOCUMENT itself says, as against a default of this build"""
    written = []
    for key in resolved:
        try:
            container, leaf = _document_slot(document, key, create=False)
        except OpsError:
            continue  # a path this document cannot hold is not one it sets
        if container is not None and leaf in container:
            written.append(key)
    return sorted(written)


def config_show(project: str | None = None, version: str | None = None,
                catalog_path: str | None = None) -> dict[str, Any]:
    """The effective configuration, and where it came from"""
    if version:
        row = _find_version(version)
        resolved = _in_scope(row["resolved"], project)
        return {"source": "version", "version": row, "project": project,
                "resolved": resolved, "drift": False,
                "written": _written_paths(row["document"], resolved)}

    file = _catalog_file(catalog_path)
    document = _read_document(file)
    resolved = _resolved_of(document, file)
    central = CentralStore()
    try:
        head = central.head_config_version()
    finally:
        central.close()
    live_id = config_version.version_id(document)
    in_scope = _in_scope(resolved, project)
    # DOCUMENTS, not ids — the third reader of this comparison
    drift = head is None or config_version.canonicalise(
        head["document"]) != config_version.canonicalise(document)
    return {"source": "file", "catalog": str(file), "project": project,
            "resolved": in_scope, "version": head,
            "file_version": live_id,
            "written": _written_paths(document, in_scope),
            "drift": drift}


def wiring_config(project: str | None = None,
                  catalog_path: str | None = None) -> Any:
    """The `WiringConfig` a scope runs under — a project's, or the fleet's base"""
    catalog = resolve_catalog(catalog_path)
    if project is None:
        return catalog.os.wiring
    return project_spec(catalog, project).wiring


def wiring_show(project: str | None = None, *, refresh: bool = False,
                block: bool = True,
                catalog_path: str | None = None) -> dict[str, Any]:
    """What the user has configured, and which of it this scope wires"""
    from . import wiring as wiring_mod

    cfg = wiring_config(project, catalog_path)
    inv = (wiring_mod.discover(refresh=refresh) if block
           else wiring_mod.cached(refresh=refresh))
    if inv is None:
        return {"project": project, "reading": True, "items": [],
                "errors": [], "unwired": [], "ts": 0.0}
    inv = wiring_mod.applied(cfg, inv)
    return {"project": project, "reading": False,
            "items": inv.items, "errors": inv.errors, "ts": inv.ts,
            "unwired": [i.name for i in inv.unwired]}


def set_wiring(lever: str, wired: bool, project: str | None = None, *,
               reason: str = "", catalog_path: str | None = None,
               actor: str = "user") -> dict[str, Any]:
    """Wire or unwire one thing for one scope — `jarvis config set` underneath"""
    from . import wiring as wiring_mod

    cfg = wiring_config(project, catalog_path)
    try:
        path, value = wiring_mod.lever_setting(lever, wired, cfg)
    except ValueError as e:
        raise OpsError(str(e)) from e
    if project is None:
        path = f"os.{path}"
    return set_config(path, value, project, reason=reason,
                      catalog_path=catalog_path, actor=actor)


def config_history(project: str | None = None,
                   limit: int = 20) -> list[dict[str, Any]]:
    """The ledger, newest first, each row flagged `head` if it is the applied one"""
    central = CentralStore()
    try:
        head = central.head_config_version()
        rows = central.config_versions(limit=max(limit * 4, limit))
    finally:
        central.close()
    out = []
    for row in rows:
        if not _touches([c["path"] for c in row["changes"]], project):
            continue
        row["head"] = bool(head and head["id"] == row["id"])
        out.append(row)
        if len(out) >= limit:
            break
    return out


def config_diff(a: str, b: str) -> dict[str, Any]:
    """Every path where two stored versions disagree, over the RESOLVED maps"""
    left, right = _find_version(a), _find_version(b)
    return {"a": {k: left[k] for k in ("id", "ts", "actor", "reason")},
            "b": {k: right[k] for k in ("id", "ts", "actor", "reason")},
            "changes": config_version.diff(left["resolved"], right["resolved"])}


def restore_config(version_id: str, *, reason: str = "",
                   catalog_path: str | None = None,
                   actor: str = "user") -> dict[str, Any]:
    """Put an old document back"""
    _refuse_worker_write("restore")
    row = _find_version(version_id)
    file = _catalog_file(catalog_path)
    try:
        before = _resolved_of(_read_document(file), file)
    except OpsError:
        before = {}
    changes = config_version.diff(before, row["resolved"])
    _require_reason(changes, reason,
                    f'jarvis config restore {row["id"]} --reason "…"')
    applied = _commit_document(row["document"], path=file, actor=actor, reason=reason,
                               changes=changes)
    return {"version": applied, "restored": row["id"], "changes": changes,
            "catalog": str(file),
            "classes": sorted({apply_class(c["path"]) for c in changes})}


def adopt_config(*, reason: str = "", catalog_path: str | None = None,
                 actor: str = "file") -> dict[str, Any]:
    """Record a hand-edited catalog as a version, so the record catches up with the file"""
    _refuse_worker_write("adopt")
    file = _catalog_file(catalog_path)
    document = _read_document(file)
    resolved = _resolved_of(document, file)
    central = CentralStore()
    try:
        head = central.head_config_version()
    finally:
        central.close()
    # DOCUMENTS, not ids: a release-rebase row is addressed by document AND build (§6.1)
    if head is not None and config_version.canonicalise(
            head["document"]) == config_version.canonicalise(document):
        return {"adopted": False, "version": head, "changes": [],
                "catalog": str(file),
                "note": "the file is already the head version — nothing to adopt"}
    changes = config_version.diff(head["resolved"] if head else {}, resolved)
    row = _commit_document(document, path=file, actor=actor, reason=reason,
                           changes=changes)
    return {"adopted": True, "version": row, "changes": changes, "catalog": str(file),
            "note": "recorded the file as a version"
                    + ("" if head else " — the ledger's first")}


# -- deferral

def defer(wo_id: str, title: str, why: str, description: str = "",
          neo_question_id: int | None = None,
          project_name: str | None = None) -> dict[str, Any]:
    """(Workers) hand work found on the way to whoever owns deciding about it"""
    from . import bus

    if not why.strip():
        raise OpsError(
            "--why is the whole argument for deferring: it is what a reader sees months "
            "later when deciding whether the item is still worth doing")
    name, path, wo = find_work_order(wo_id, project_name)
    store = ProjectStore(path)
    try:
        env_id = bus.post(
            store, subject=bus.Subject(wo_id=wo_id), from_role="implementor",
            to_role="manager",
            payload=bus.DeferralRequest(title=title, why=why,
                                        neo_question_id=neo_question_id,
                                        description=description))
        # On the work order's own record
        store.add_event(wo_id, "deferral_submitted",
                        {"title": title, "why": why, "envelope_id": env_id,
                         "neo_question_id": neo_question_id})
    finally:
        store.close()
    return {"project": name, "wo_id": wo_id, "envelope_id": env_id, "title": title,
            "note": "deferral submitted — it is out of your hands now; carry on with "
                    "your work order"}


# -- backlog

def promote_backlog(item_id: str, force: bool = False,
                    as_feature: bool = False,
                    max_parallel: int | None = None) -> dict[str, Any]:
    """Turn an intake item into committed work"""
    if max_parallel is not None and not as_feature:
        # Refused rather than ignored: a work order has no children to cap
        raise OpsError("--max-parallel applies to a feature order; add --as feature")
    central = CentralStore()
    try:
        item = central.get_backlog(item_id)
        if not item:
            raise OpsError(f"backlog item {item_id!r} not found")
        if item["status"] != "open":
            raise OpsError(f"backlog item {item_id} is {item['status']}, not open")
        blockers = central.unfinished_dependencies(item_id)
        if blockers and not force:
            raise OpsError(
                f"backlog item {item_id} has unfinished dependencies: "
                + ", ".join(f"{b['id']} ({b['status']})" for b in blockers)
                + " — finish them first or use --force"
            )
        if as_feature:
            fo = create_feature_order(
                item["project"], item["title"], description=item["description"],
                origin="jarvis", backlog_id=item_id, max_parallel=max_parallel,
            )
            # `promoted_wo_id` takes the feature order's id: the column records what the item BECAME
            central.mark_backlog(item_id, "promoted", promoted_wo_id=fo["id"])
            return {"backlog_id": item_id, "fo_id": fo["id"],
                    "project": item["project"],
                    "forced_over_blockers": [b["id"] for b in blockers] if force else [],
                    "note": "a planner will decompose it; the plan comes back for "
                            "review before any work order is created"}
        wo = create_work_order(
            item["project"], item["title"], description=item["description"],
            origin="jarvis", backlog_id=item_id,
        )
        central.mark_backlog(item_id, "promoted", promoted_wo_id=wo["id"])
        return {"backlog_id": item_id, "wo_id": wo["id"], "project": item["project"],
                "forced_over_blockers": [b["id"] for b in blockers] if force else []}
    finally:
        central.close()


# -- token accounting

#: The turn states whose spend is final
_SETTLED_TURN_STATES = ("done", "failed")


def _turn_usage(store: ProjectStore, turn: dict[str, Any],
                previous: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """A settled turn's recorded usage envelope, lazily (re-)derived from its outfile"""
    from . import claude_cli

    raw = turn.get("usage_json")
    stored = db.from_json(raw, None) if raw else None
    if isinstance(stored, dict) and \
            stored.get("usage_v", 1) >= claude_cli.USAGE_SCHEMA_VERSION:
        return stored
    if turn.get("state") not in _SETTLED_TURN_STATES or not turn.get("outfile"):
        return stored
    result = claude_cli.read_turn_result(Path(turn["outfile"]), previous=previous)
    if result is None or not result.usage:
        return stored
    store.set_turn_usage(turn["id"], db.to_json(result.usage),
                         cost_usd=result.cost_usd)
    turn["cost_usd"] = result.cost_usd
    return result.usage


def _turn_row(turn: dict[str, Any], u: dict[str, Any] | None) -> dict[str, Any]:
    """One turn, flattened for the per-turn table"""
    duration = None
    if turn.get("ended_at") and turn.get("started_at"):
        duration = round(turn["ended_at"] - turn["started_at"], 1)
    row = {
        "seq": turn["seq"], "kind": turn["kind"], "state": turn["state"],
        "started_at": turn["started_at"], "ended_at": turn.get("ended_at"),
        "duration_s": duration, "cost_usd": turn.get("cost_usd"),
        # Which reading billed this turn (`project_store.COST_FROM_*`) (#471)
        "cost_source": turn.get("cost_source"),
        "recorded": u is not None,
        # WHICH CACHE-TTL ARM this turn ran on (docs/superpowers/specs/2026-10-15-the-hour-on-worker-turns-only.md)
        "cache_ttl": turn.get("cache_ttl") or "",
        # Which message set this turn going, where one did
        "msg_id": turn.get("msg_id"),
    }
    if u is None:
        return row
    window = u.get("context_window") or 0
    peak = u.get("context_peak") or 0
    row.update({
        "cost_usd": turn["cost_usd"] if turn.get("cost_usd") is not None
        else u.get("total_cost_usd"),
        # Which reading of the result envelope these numbers came from
        "usage_v": u.get("usage_v") or 1,
        # The turn's spend split by the model that served it
        "by_model": u.get("by_model") or [],
        "input": u.get("input") or 0,
        "cache_write": u.get("cache_write") or 0,
        "cache_read": u.get("cache_read") or 0,
        "cache_1h": u.get("cache_1h") or 0,
        "cache_5m": u.get("cache_5m") or 0,
        "output": u.get("output") or 0,
        "api_calls": u.get("api_calls"),
        "context_peak": peak,
        "context_window": window or None,
        # The /context statistic: how full the model's window was at this turn's largest call
        "context_pct": round(100 * peak / window, 1) if window else None,
        "duration_api_ms": u.get("duration_api_ms"),
    })
    return row


def _turn_rows(store: ProjectStore, wo_id: str) -> list[dict[str, Any]]:
    """Every turn of one work order, in order"""
    rows: list[dict[str, Any]] = []
    previous: dict[str, Any] | None = None
    for turn in store.list_turns(wo_id):
        envelope = _turn_usage(store, turn, previous)
        rows.append(_turn_row(turn, envelope))
        if envelope is not None:
            previous = envelope
    return rows


def _turn_summary(rows: list[dict[str, Any]]) -> tuple[str, int, int,
                                                       dict[str, Any] | None]:
    """(provenance, recorded, settled, exact totals) over one work order's turns"""
    settled = [r for r in rows if r["state"] in _SETTLED_TURN_STATES]
    recorded = [r for r in rows if r["recorded"]]
    if recorded and len(recorded) == len(settled):
        provenance = "recorded"
    elif recorded:
        provenance = "mixed"
    else:
        provenance = "transcript"
    totals = None
    if recorded:
        windows = [r["context_window"] for r in recorded if r.get("context_window")]
        totals = {
            # Exact aggregation IS the sum of the turns — nothing is re-derived
            "cost_usd": round(sum(r["cost_usd"] or 0 for r in recorded), 4),
            "input": sum(r["input"] for r in recorded),
            "cache_write": sum(r["cache_write"] for r in recorded),
            "cache_read": sum(r["cache_read"] for r in recorded),
            "cache_1h": sum(r["cache_1h"] for r in recorded),
            "cache_5m": sum(r["cache_5m"] for r in recorded),
            "output": sum(r["output"] for r in recorded),
            "api_calls": sum(r["api_calls"] or 0 for r in recorded),
            "context_peak": max(r["context_peak"] for r in recorded),
            "context_window": max(windows) if windows else None,
        }
    return provenance, len(recorded), len(settled), totals


def _partition_calls(
    groups: Sequence[dict[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Split one work order's `agent_calls` groups into (the OS's, the worker's own) (#103)"""
    from . import agent_usage

    os_side, worker_side = [], []
    for g in groups:
        kind = g.get("kind") or ""
        # A METERED LOOK IS NEITHER, and is dropped from both: §10's rows are not `claude` calls at all (zero... (docs/superpowers/specs/2026-09-24-order-observability.md §10)
        if agent_usage.is_observability(kind):
            continue
        target = worker_side if agent_usage.is_subprocess(kind) else os_side
        target.append(g)
    return os_side, worker_side


def _os_spend(groups: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """One work order's OS-side spend, from its `agent_calls` groups"""
    return _call_spend(groups, "os")


def _subproc_spend(groups: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """The same arithmetic, over the `claude` processes the WORKER spawned beneath itself"""
    return _call_spend(groups, "subproc")


def _priced_group(usage_mod: Any, g: dict[str, Any]) -> Any:
    """One `agent_call_totals` group at list prices, TTL SPLIT INCLUDED"""
    return usage_mod.priced(
        g.get("model") or "unknown", input=g.get("input") or 0,
        cache_write=g.get("cache_write") or 0, cache_read=g.get("cache_read") or 0,
        output=g.get("output") or 0, messages=g.get("calls") or 0,
        cache_1h=g.get("cache_1h") or 0, cache_5m=g.get("cache_5m") or 0)


def _call_spend(groups: Sequence[dict[str, Any]], prefix: str) -> dict[str, Any]:
    """Sum and price one class of `agent_calls` groups under `<prefix>_…` keys"""
    from . import agent_usage
    from . import usage as usage_mod

    total = usage_mod.Usage()
    by_kind: dict[str, dict[str, Any]] = {}
    calls = failed = 0
    exact = 0.0
    largest_input = 0
    for g in groups:
        u = _priced_group(usage_mod, g)
        total = total + u
        calls += g.get("calls") or 0
        failed += g.get("failed") or 0
        exact += g.get("cost_usd") or 0.0
        kind = g.get("kind") or "other"
        entry = by_kind.setdefault(kind, {"kind": kind, "label": agent_usage.describe(kind),
                                          "calls": 0, "cost_usd": 0.0,
                                          "billed_input": 0, "output": 0,
                                          "max_input_chars": 0})
        entry["calls"] += g.get("calls") or 0
        entry["cost_usd"] = round(entry["cost_usd"] + u.list_cost_usd, 4)
        entry["billed_input"] += u.billed_input
        entry["output"] += u.output
        # A MAX of one call's TOTAL input (docs/superpowers/specs/2026-09-26-bounded-model-inputs.md)
        biggest = g.get("max_input_chars") or 0
        entry["max_input_chars"] = max(entry["max_input_chars"], biggest)
        largest_input = max(largest_input, biggest)
    return {
        f"{prefix}_max_input_chars": largest_input,
        f"{prefix}_calls": calls,
        f"{prefix}_failed_calls": failed,
        f"{prefix}_cost_usd": round(total.list_cost_usd, 4),
        f"{prefix}_recorded_cost_usd": round(exact, 4),
        f"{prefix}_billed_input": total.billed_input,
        f"{prefix}_output": total.output,
        f"{prefix}_total_tokens": total.total_tokens,
        # Carried up for the footer's write-TTL line; see `_write_ttl`
        f"{prefix}_cache_write": total.cache_write,
        f"{prefix}_cache_1h": total.cache_1h,
        f"{prefix}_cache_5m": total.cache_5m,
        # Dearest first: the whole point of the split is to say where the spend goes
        f"{prefix}_by_kind": sorted(by_kind.values(), key=lambda k: -k["cost_usd"]),
    }


def _unit_row(name: str, wo: dict[str, Any], index: dict[str, list[Path]],
              turn_rows: Sequence[dict[str, Any]] = (),
              os_groups: Sequence[dict[str, Any]] = (),
              *, since: float | None = None,
              until: float | None = None) -> dict[str, Any]:
    """One work order's spend, flattened for a table"""
    from . import usage as usage_mod

    from .bill import _cold_prefix_floor

    session = usage_mod.read_session(wo.get("session_id") or "", _cold_prefix_floor(),
                                     index=index, since=since, until=until)
    total = session.total
    provenance, recorded, settled, rec_totals = _turn_summary(list(turn_rows))
    os_groups_only, subproc_groups = _partition_calls(list(os_groups))
    os_spend = _os_spend(os_groups_only)
    subproc_spend = _subproc_spend(subproc_groups)
    worker_cost = total.list_cost_usd if session.found else 0.0
    recorded_cost = round(rec_totals["cost_usd"], 4) if rec_totals else 0.0
    return {
        "id": wo["id"], "project": name, "title": wo["title"],
        "status": wo["status"], "kind": wo.get("kind") or "worker",
        "found": session.found,
        # A session's turn count is its resume boundaries plus the opening turn
        "turns": total.resume_boundaries + 1 if session.found else 0,
        "subagent_count": session.subagent_count,
        "subagent_cost_usd": round(session.subagents.list_cost_usd, 2),
        "provenance": provenance,
        "recorded_turns": recorded,
        "settled_turns": settled,
        "recorded_cost_usd": recorded_cost,
        **os_spend,
        **subproc_spend,
        # Estimate + estimate + estimate, all at list prices
        "total_cost_usd": round(worker_cost + os_spend["os_cost_usd"]
                                + subproc_spend["subproc_cost_usd"], 4),
        "total_recorded_cost_usd": round(recorded_cost
                                         + os_spend["os_recorded_cost_usd"]
                                         + subproc_spend["subproc_recorded_cost_usd"], 4),
        # Is there anything to show at all?
        "measurable": bool(session.found or os_spend["os_calls"]
                           or subproc_spend["subproc_calls"] or recorded),
        **total.as_dict(),
    }


#: Why every figure in a cost report is a lower bound
COST_FLOOR_NOTE = (
    "a floor: `claude` processes a worker spawns outside Jarvis's own transport "
    "(a bare `claude -p` from a shell) leave nothing behind that names a work order"
)


def bill(target: str, project: str | None = None, *,
         live: bool = False) -> dict[str, Any]:
    """The itemised bill for one order — see `jarvis.bill`"""
    from .bill import build

    return build(target, project, live=live)


def fleet_cost(**kwargs: Any) -> dict[str, Any]:
    """What a TYPICAL order costs, over a window — see `jarvis.fleetcost`"""
    from . import fleetcost

    return fleetcost.report(**kwargs)


def cost_window(**kwargs: Any) -> dict[str, Any]:
    """Which window a cost surface is reporting over — see `fleetcost.resolve_window` (docs/superpowers/specs/2026-10-07-cost-window-selector.md)"""
    from . import fleetcost

    project = kwargs.pop("project", None)
    kwargs.setdefault("cfg", fleetcost.cost_config(project))
    return fleetcost.resolve_window(**kwargs)


def cost_meter(**kwargs: Any) -> dict[str, Any]:
    """The account's usage meter reconciled against measured spend — see `usage_meter.reconciliation`"""
    from . import usage_meter

    return usage_meter.reconciliation(**kwargs)


def meter_samples(**kwargs: Any) -> list[dict[str, Any]]:
    """The raw usage-meter series for one span — see `usage_meter.sample_rows`"""
    from . import usage_meter

    return usage_meter.sample_rows(**kwargs)


def cost_zone(tz: str | None = None, project: str | None = None) -> str:
    """Which zone a cost surface DISPLAYS in — see `fleetcost.resolve_zone` (docs/superpowers/specs/2026-10-07-cost-window-selector.md)"""
    from . import fleetcost

    return fleetcost.resolve_zone(tz, fleetcost.cost_config(project))


def cost_report(project: str | None = None, target: str | None = None,
                limit: int = 50, include_hidden: bool = True,
                window: dict[str, Any] | None = None) -> dict[str, Any]:
    """What the fleet's work has cost in tokens, read back from Claude Code's transcripts (#103)"""
    from . import usage as usage_mod

    index = usage_mod.index_sessions()
    paths = registered_project_paths()
    if project and project not in paths:
        raise OpsError(f"project {project!r} not registered (known: {sorted(paths)})")
    if target:
        return _cost_for_target(target, project, index)

    since = window["since"] if window else None
    until = window["until"] if window else None
    os_groups = _os_groups(project, since=since, until=until)
    scope = {project: paths[project]} if project else paths
    units: list[dict[str, Any]] = []
    for name, path in sorted(scope.items()):
        if not path.is_dir():
            continue
        store = ProjectStore(path)
        try:
            rows = store.list_work_orders(
                limit=limit, include_hidden=include_hidden,
                active_between=(since, until) if window else None)
            # An order whose only in-window activity is an `agent_call`
            seen = {wo["id"] for wo in rows}
            for wo_id in _os_only_ids(os_groups, name, seen) if window else ():
                try:
                    extra = store.get_work_order(wo_id)
                except KeyError:
                    continue            # a legacy row naming no project: not this one's
                if include_hidden or not extra["hidden"]:
                    rows.append(extra)
            for wo in rows:
                units.append(_unit_row(name, wo, index, _turn_rows(store, wo["id"]),
                                       os_groups.get(wo["id"], ()),
                                       since=since, until=until))
        finally:
            store.close()
    # Dearest first, counting what Jarvis spent on the order as part of what it cost
    units.sort(key=lambda u: u["total_cost_usd"], reverse=True)
    # ADDITIVE, and the version is not bumped: the existing `measured`/`unmeasured`/ `totals` keys...
    return {"scope": project or "fleet", "units": units, "window": window,
            **_rollup(units), "os_unattributed": _os_unattributed(os_groups),
            "floor": True, "floor_reason": COST_FLOOR_NOTE}


def _os_groups(project: str | None = None, *, since: float | None = None,
               until: float | None = None) -> dict[str, list[dict[str, Any]]]:
    """Every work order's OS-side call groups, in one query"""
    central = CentralStore()
    try:
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in central.agent_call_totals(project, since=since, until=until):
            groups.setdefault(row["wo_id"] or "", []).append(row)
        return groups
    finally:
        central.close()


def _os_only_ids(groups: dict[str, list[dict[str, Any]]], project: str,
                 seen: set[str]) -> list[str]:
    """Ids whose only in-window activity was an OS call, NARROWED TO ONE PROJECT (docs/superpowers/specs/2026-10-07-cost-window-selector.md)"""
    return sorted(
        wo_id for wo_id, rows in groups.items()
        if wo_id and wo_id not in seen
        and any((r.get("project") or "") in ("", project) for r in rows))


def _os_unattributed(groups: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """OS spend that no work order caused (`agent_calls.wo_id = ''`)"""
    return _os_spend(groups.get("", []))


def _rollup(units: list[dict[str, Any]]) -> dict[str, Any]:
    """Totals over the units that could actually be measured"""
    measured = [u for u in units if u["found"]]
    worker_cost = round(sum(u["list_cost_usd"] for u in measured), 2)
    # Over ALL units, not just measured ones
    os_cost = round(sum(u.get("os_cost_usd") or 0 for u in units), 2)
    subproc_cost = round(sum(u.get("subproc_cost_usd") or 0 for u in units), 2)
    return {
        "measured": len(measured),
        "unmeasured": len(units) - len(measured),
        "totals": {
            # Over ALL units, not just measured ones: the recorded figure comes from the OS's own turn rows
            "recorded_cost_usd": round(
                sum(u.get("recorded_cost_usd") or 0 for u in units), 2),
            "list_cost_usd": worker_cost,
            # The headline: workers, everything the OS spent on their behalf
            "total_cost_usd": round(worker_cost + os_cost + subproc_cost, 2),
            "os_cost_usd": os_cost,
            "os_recorded_cost_usd": round(
                sum(u.get("os_recorded_cost_usd") or 0 for u in units), 2),
            "os_calls": sum(u.get("os_calls") or 0 for u in units),
            "os_billed_input": sum(u.get("os_billed_input") or 0 for u in units),
            # The biggest OS-side input on the whole report (docs/superpowers/specs/2026-09-26-bounded-model-inputs.md)
            "os_max_input_chars": max(
                [u.get("os_max_input_chars") or 0 for u in units], default=0),
            "os_output": sum(u.get("os_output") or 0 for u in units),
            "subproc_cost_usd": subproc_cost,
            "subproc_recorded_cost_usd": round(
                sum(u.get("subproc_recorded_cost_usd") or 0 for u in units), 2),
            "subproc_calls": sum(u.get("subproc_calls") or 0 for u in units),
            "subproc_billed_input": sum(
                u.get("subproc_billed_input") or 0 for u in units),
            "subproc_output": sum(u.get("subproc_output") or 0 for u in units),
            "rewrite_cost_usd": round(sum(u["rewrite_cost_usd"] for u in measured), 2),
            "rewrite_excess": sum(u["rewrite_excess"] for u in measured),
            "resume_boundaries": sum(u["resume_boundaries"] for u in measured),
            "subagent_cost_usd": round(sum(u["subagent_cost_usd"] for u in measured), 2),
            "output": sum(u["output"] for u in measured),
            "billed_input": sum(u["billed_input"] for u in measured),
            # Over ALL units: what a window could not place is disclosed whether or not the order it belongs to...
            "undated_messages": sum(u.get("undated_messages") or 0 for u in units),
            **_write_ttl(measured, units),
        },
    }


def _write_ttl(measured: list[dict[str, Any]], units: list[dict[str, Any]]
               ) -> dict[str, int]:
    """Cache-write tokens and their TTL split, over every class of spend on the report"""
    out = {"cache_write": 0, "cache_1h": 0, "cache_5m": 0}
    for key in out:
        out[key] = (sum(u.get(key) or 0 for u in measured)
                    + sum((u.get(f"os_{key}") or 0) + (u.get(f"subproc_{key}") or 0)
                          for u in units))
    return out


def _cost_for_target(target: str, project: str | None,
                     index: dict[str, list[Path]]) -> dict[str, Any]:
    """One work order, or a feature order rolled up over its planner and children"""
    try:
        name, path, fo = find_feature_order(target, project)
    except OpsError:
        name, wo_path, wo = find_work_order(target, project)
        store = ProjectStore(wo_path)
        try:
            rows = _turn_rows(store, wo["id"])
        finally:
            store.close()
        os_groups = _os_groups()
        wo_groups = os_groups.get(wo["id"], ())
        unit = _unit_row(name, wo, index, rows, wo_groups)
        provenance, recorded, settled, rec_totals = _turn_summary(rows)
        # The per-turn breakdown is the single-work-order payload: it is what shows WHERE in a bloated work...
        return {"scope": target, "units": [unit], **_rollup([unit]),
                "turns_detail": rows, "provenance": provenance,
                "turns_recorded": recorded, "turns_settled": settled,
                "recorded_totals": rec_totals,
                "os_calls_detail": _os_calls_detail(wo["id"]),
                "subproc_detail": _subproc_detail(_partition_calls(list(wo_groups))[1]),
                "floor": True, "floor_reason": COST_FLOOR_NOTE}

    os_groups = _os_groups()
    store = ProjectStore(path)
    try:
        units = []
        planner_id = fo.get("plan_wo_id")
        if planner_id:
            try:
                units.append(_unit_row(name, store.get_work_order(planner_id), index,
                                       _turn_rows(store, planner_id),
                                       os_groups.get(planner_id, ())))
            except KeyError:
                pass
        units.extend(_unit_row(name, child, index, _turn_rows(store, child["id"]),
                               os_groups.get(child["id"], ()))
                     for child in store.feature_children(fo["id"], kinds=WORKER_CHILDREN))
    finally:
        store.close()
    return {"scope": fo["id"], "title": fo["title"], "status": fo["status"],
            "units": units, **_rollup(units),
            "floor": True, "floor_reason": COST_FLOOR_NOTE}


def inspect_config(project: str | None = None) -> Any:
    """The `jarvis inspect` settings in force for `project` — or the OS's — or defaults"""
    from .catalog import InspectConfig

    try:
        catalog = resolve_catalog()
        return (catalog.os.inspect if project is None
                else catalog.project(project).inspect)
    except (OpsError, CatalogError, OSError, ValueError):
        return InspectConfig()


def ui_health_config(project: str | None = None) -> Any:
    """The dashboard's liveness settings in force for `project` — or the OS's — or defaults (docs/superpowers/specs/2026-10-08-the-dashboard-reports-and-heals-its-own-wedge.md)"""
    from .catalog import UiHealthConfig

    try:
        catalog = resolve_catalog()
        return (catalog.os.ui_health if project is None
                else catalog.project(project).ui_health)
    except (OpsError, CatalogError, OSError, ValueError):
        return UiHealthConfig()


def navigation_config(project: str | None = None) -> Any:
    """The `jarvis navigation` settings in force for `project` — or the OS's — or defaults"""
    from .catalog import NavigationConfig

    try:
        catalog = resolve_catalog()
        return (catalog.os.navigation if project is None
                else catalog.project(project).navigation)
    except (OpsError, CatalogError, OSError, ValueError):
        return NavigationConfig()


#: Why a no-argument `jarvis navigation` refuses
NAVIGATION_NEEDS_SCOPE = (
    "name a work order, a feature order or a project — or pass --fleet. A no-argument "
    "run would walk every transcript Claude Code has ever written (2.7G, 11,889 lead "
    "files on this box) to answer a question that is usually about one order."
)


#: Why `jarvis navigation --days 0` refuses
NAVIGATION_DAYS_POSITIVE = (
    "--days must be a positive number of days. `--days 0` would REMOVE the window "
    "rather than narrow it, and walk every transcript Claude Code has ever written "
    "(2.7G, 11,889 lead files on this box). Omit --days for the project's configured "
    "window."
)


#: What `_resolve_report_target` returns in the first slot
_TARGET_FEATURE = "feature_order"
_TARGET_WORK = "work_order"
_TARGET_PROJECT = "<project>"

#: How an order id is spelled, taken from `_resolve_order_id` (ops.py:239) and...
_ORDER_ID_PREFIXES = ("wo-", "fo-")


def _resolve_report_target(target: str, project: str | None = None, *,
                           project_fallback: bool = False,
                           ) -> tuple[str, str | None, str | None,
                                      dict[str, Any] | None]:
    """What one id means to a report: `(kind, project name, project path, record)`"""
    try:
        name, path, fo = find_feature_order(target, project)
    except OpsError:
        pass
    else:
        return _TARGET_FEATURE, name, path, fo
    try:
        name, path, wo = find_work_order(target, project)
    except OpsError:
        if not project_fallback or target.startswith(_ORDER_ID_PREFIXES):
            raise
        return _TARGET_PROJECT, None, None, None
    return _TARGET_WORK, name, path, wo


def navigation_report(target: str | None = None, project: str | None = None, *,
                      fleet: bool = False, days: int | None = None) -> dict[str, Any]:
    """How an order, a project or the fleet NAVIGATED code — `jarvis navigation`"""
    from . import nav_volume
    from . import usage as usage_mod

    if not target and not project and not fleet:
        raise OpsError(NAVIGATION_NEEDS_SCOPE)
    if days is not None and days <= 0:
        raise OpsError(NAVIGATION_DAYS_POSITIVE)

    scope_project = project
    session_ids: list[tuple[str, str]] = []     # (label, session id)
    if target:
        # The one resolver `inspect_report` uses, plus the project reading only this command has
        kind, name, path, record = _resolve_report_target(
            target, project, project_fallback=True)
        if kind == _TARGET_FEATURE:
            store = ProjectStore(path)
            try:
                ids = []
                planner_id = record.get("plan_wo_id")
                if planner_id:
                    try:
                        ids.append(store.get_work_order(planner_id))
                    except KeyError:
                        pass
                ids.extend(store.feature_children(record["id"], kinds=WORKER_CHILDREN))
            finally:
                store.close()
            scope_project = scope_project or name
            session_ids = [(wo["id"], wo.get("session_id") or "") for wo in ids]
            scope = record["id"]
        elif kind == _TARGET_WORK:
            scope_project = scope_project or name
            session_ids = [(record["id"], record.get("session_id") or "")]
            scope = record["id"]
        else:
            scope_project = target
            session_ids = []
            scope = target
    else:
        scope = scope_project or "fleet"

    cfg = navigation_config(scope_project)
    window = cfg.window_days if days is None else days

    if session_ids:
        index = usage_mod.index_sessions()
        rolled = nav_volume.NavigationVolume(scope=scope)
        for _label, session in session_ids:
            if not session:
                continue
            rolled.fold(nav_volume.read_session(session, cfg, index=index))
        return rolled.as_dict()

    # A project or the fleet: the tree, within the window
    prefix = ""
    siblings: list[str] = []
    if scope_project:
        catalog = resolve_catalog()
        prefix = nav_volume.slug_of(project_spec(catalog, scope_project).path)
        # The OTHER projects' slugs: `slug_of` is not injective
        siblings = [nav_volume.slug_of(spec.path) for spec in catalog.projects
                    if spec.name != scope_project]
    volume = nav_volume.read_tree(None, cfg, days=window, slug_prefix=prefix,
                                  slug_exclude=siblings)
    payload = volume.as_dict()
    payload["scope"] = scope
    return payload


def _spec_scope(project: str | None) -> tuple[str, Path]:
    """Which project's tree `jarvis spec` reads, and never more than one — §4.2"""
    paths = registered_project_paths()
    if project:
        if project not in paths:
            known = ", ".join(sorted(paths)) or "no project is registered"
            raise OpsError(
                f"no registered project named {project!r} — `jarvis spec` scans one "
                f"project at a time, never a list. Registered: {known}"
            )
        return project, paths[project]
    cwd = os.getcwd()
    name = _project_for_cwd(cwd, paths)
    if not name:
        raise OpsError(
            f"no registered project owns {cwd} — pass --project <name> to say which "
            f"tree to read"
        )
    return name, paths[name]


def _spec_file(root: Path, path: str) -> tuple[Path, str]:
    """A document inside `root`, or an OpsError naming the root it refused to leave"""
    candidate = Path(path)
    target = candidate if candidate.is_absolute() else Path(os.getcwd()) / candidate
    resolved = target.resolve()
    base = root.resolve()
    if not resolved.is_relative_to(base):
        raise OpsError(
            f"{path!r} resolves to {resolved}, outside {base} — `jarvis spec` refuses to "
            f"leave the project root"
        )
    if not resolved.is_file():
        raise OpsError(f"no such file in {base}: {path}")
    return resolved, resolved.relative_to(base).as_posix()


def _spec_callable_path(resolved: Path) -> str:
    """A path string that opens `resolved` FROM THE CALLER'S CWD, for the printed command"""
    rel = os.path.relpath(resolved, os.getcwd())
    return str(resolved) if ".." in Path(rel).parts else rel


def _spec_command(path: str, ref: str, project: str | None = None) -> str:
    """The exact command that shows one row — the way `jarvis search` prints one per hit"""
    import shlex

    scope = f" --project {shlex.quote(project)}" if project is not None else ""
    return f"jarvis spec section{scope} {shlex.quote(path)} {shlex.quote(ref)}" if ref \
        else f"jarvis spec toc{scope} {shlex.quote(path)}"


def spec_toc(path: str, project: str | None = None) -> dict[str, Any]:
    """What is in this document and how big each part is — `jarvis spec toc` (§4.2)"""
    from . import spec_index

    name, root = _spec_scope(project)
    resolved, rel = _spec_file(root, path)
    text = resolved.read_text(encoding="utf-8", errors="replace")
    callable_path = _spec_callable_path(resolved)
    rows = []
    for s in spec_index.toc(text):
        ref = s.number or s.name
        rows.append({"number": s.number, "name": s.name, "level": s.level,
                     "line": s.line, "tokens_estimate": s.tokens, "ref": ref,
                     "command": _spec_command(callable_path, ref, project)})
    return {"project": name, "root": str(root), "path": rel, "sections": rows,
            "tokens_estimate": len(text) // 4}


def spec_section(path: str, which: str, project: str | None = None) -> dict[str, Any]:
    """One section of one document — `jarvis spec section` (§4.2)"""
    from . import spec_index

    name, root = _spec_scope(project)
    resolved, rel = _spec_file(root, path)
    text = resolved.read_text(encoding="utf-8", errors="replace")
    content = spec_index.section(text, which)
    if content is None:
        names = [s.name for s in spec_index.toc(text)]
        headings = ", ".join(names[:12]) + ("…" if len(names) > 12 else "")
        raise OpsError(
            f"{which!r} matches no section of {rel}. It carries: "
            f"{headings or 'no headings at all'}"
        )
    return {"project": name, "root": str(root), "path": rel, "which": which,
            "content": content, "tokens_estimate": len(content) // 4,
            "command": _spec_command(_spec_callable_path(resolved), which, project)}


def spec_search(words: str, project: str | None = None,
                limit: int = 40) -> dict[str, Any]:
    """Which SECTIONS of one project's documents match — `jarvis spec search` (§4.2)"""
    from . import spec_index

    name, root = _spec_scope(project)
    hits = spec_index.search(root, words, limit=limit)
    # `h.path` is root-relative for DISPLAY; the command needs one callable from the cwd
    base = root.resolve()
    rows = [{"path": h.path, "section": h.section, "line": h.line, "context": h.context,
             "command": _spec_command(_spec_callable_path((base / h.path).resolve()),
                                      h.section, project)} for h in hits]
    return {"project": name, "root": str(root), "words": words, "limit": limit,
            "count": len(rows), "truncated": len(rows) >= limit, "hits": rows}


def cold_prefix_floor(project: str | None = None) -> int | None:
    """`os.cold_prefix_floor` if a catalog can be reached, else None"""
    try:
        return resolve_catalog().os.cold_prefix_floor
    except (OpsError, CatalogError, OSError, ValueError):
        return None


def inspect_config_at(project_path: Path) -> Any:
    """`inspect_config` for the project rooted at `project_path`"""
    from .catalog import InspectConfig

    try:
        catalog = resolve_catalog()
        target = Path(project_path).resolve()
        for spec in catalog.projects:
            if Path(spec.path).resolve() == target:
                return spec.inspect
        return catalog.os.inspect
    except (OpsError, CatalogError, OSError, ValueError):
        return InspectConfig()


def auto_review_at(project_path: Path) -> bool:
    """Will the OS decide this project's assumptions ITSELF, rather than the user?"""
    try:
        catalog = resolve_catalog()
        if not catalog.os.neo.enabled:
            return False
        target = Path(project_path).resolve()
        cfg = catalog.os.validation
        for spec in catalog.projects:
            if Path(spec.path).resolve() == target:
                cfg = spec.validation
                break
        return bool(cfg.enabled and cfg.auto_review)
    except (OpsError, CatalogError, OSError, ValueError):
        return False


def schedule_config_at(project_path: Path, catalog: Catalog | None = None) -> Any:
    """THE ONLY WAY ANYTHING LEARNS WHETHER THIS PROJECT SCHEDULES ANYTHING"""
    from .catalog import ScheduleConfig

    try:
        cat = catalog if catalog is not None else resolve_catalog()
        target = Path(project_path).resolve()
        for spec in cat.projects:
            if Path(spec.path).resolve() == target:
                return spec.schedule
    except (OpsError, CatalogError, OSError, ValueError):
        pass
    return ScheduleConfig()


def messaging_config_at(project_path: Path) -> Any:
    """`os.messaging` for the project rooted at `project_path`"""
    from .catalog import MessagingConfig

    try:
        catalog = resolve_catalog()
        target = Path(project_path).resolve()
        for spec in catalog.projects:
            if Path(spec.path).resolve() == target:
                return spec.messaging
        return catalog.os.messaging
    except (OpsError, CatalogError, OSError, ValueError):
        return MessagingConfig()


def pull_requests_config_at(project_path: Path) -> Any:
    """`os.pull_requests` for the project rooted at `project_path`"""
    from .catalog import PullRequestsConfig

    try:
        catalog = resolve_catalog()
        target = Path(project_path).resolve()
        for spec in catalog.projects:
            if Path(spec.path).resolve() == target:
                return spec.pull_requests
        return catalog.os.pull_requests
    except (OpsError, CatalogError, OSError, ValueError):
        return PullRequestsConfig()


@observability.metered(OBSERVE_INSPECT, target="target", project="project")
def inspect_report(target: str, project: str | None = None, *,
                   write_floor: int | None = None,
                   join_floor: int | None = None,
                   with_navigation: bool = False) -> dict[str, Any]:
    """Where a work order's or a feature order's TIME went — `jarvis cost`'s other half"""
    from dataclasses import replace

    from . import autopsy, holds, nav_volume
    from . import usage as usage_mod

    index = usage_mod.index_sessions()
    # One reader, two surfaces (q1216): the per-order navigation section is a key on this report as well...
    nav_configs: dict[str, Any] = {}

    def nav_settings(project_name: str) -> Any:
        if project_name not in nav_configs:
            nav_configs[project_name] = navigation_config(project_name)
        return nav_configs[project_name]
    # Resolved ONCE for the whole report
    floor = cold_prefix_floor()
    # The catalog decides the floors and the flags override them for one invocation: a project's setting...
    configs: dict[str, Any] = {}

    def settings(project_name: str) -> Any:
        if project_name not in configs:
            cfg = inspect_config(project_name)
            overrides = {}
            if write_floor is not None:
                overrides["report_write_floor"] = write_floor
            if join_floor is not None:
                overrides["report_join_floor"] = join_floor
            configs[project_name] = replace(cfg, **overrides) if overrides else cfg
        return configs[project_name]

    def unit(project_name: str, wo: dict[str, Any],
             store: ProjectStore) -> dict[str, Any]:
        session = wo.get("session_id") or ""
        cfg = settings(project_name)
        # The OS's own record of what it was holding this order for
        spans = holds.held(store, wo["id"])
        # Spec 2026-09-27 §4: the ONE chokepoint, which prefers the seal and says which reading answered
        anatomy, provenance = autopsy.anatomy_for(
            wo, cfg, spans=spans, index=index,
            turn_starts=store.turn_starts(wo["id"]), cold_prefix_floor=floor)
        payload = anatomy.as_dict()
        if with_navigation:
            # §2.2: the per-order navigation volume, same reader as `jarvis navigation`'s
            payload["navigation"] = nav_volume.read_session(
                session, nav_settings(project_name), index=index).as_dict()
        payload.update(provenance=provenance,
                       wo_id=wo["id"], project=project_name, title=wo["title"],
                       status=wo["status"], kind=wo.get("kind") or "worker",
                       # The biggest input Jarvis itself sent on this order's behalf (spec §3 (docs/superpowers/specs/2026-09-26-bounded-model-inputs.md)
                       largest_os_input=_largest_os_input(wo["id"]))
        return payload

    # The one resolver `jarvis navigation` uses
    kind, name, path, record = _resolve_report_target(target, project)
    if kind == _TARGET_WORK:
        wo = record
        cfg = settings(name)
        store = ProjectStore(path)
        try:
            payload = unit(name, wo, store)
        finally:
            store.close()
        return {"scope": wo["id"], "title": wo["title"],
                "write_floor": cfg.report_write_floor,
                "join_floor": cfg.report_join_floor, "units": [payload]}

    fo = record
    store = ProjectStore(path)
    try:
        units = []
        planner_id = fo.get("plan_wo_id")
        if planner_id:
            try:
                units.append(unit(name, store.get_work_order(planner_id), store))
            except KeyError:
                pass
        units.extend(unit(name, child, store)
                     for child in store.feature_children(fo["id"], kinds=WORKER_CHILDREN))
    finally:
        store.close()
    cfg = settings(name)
    return {"scope": fo["id"], "title": fo["title"], "status": fo["status"],
            "write_floor": cfg.report_write_floor,
            "join_floor": cfg.report_join_floor, "units": units}


@observability.metered(OBSERVE_LIVE, target="target", project="project")
def live_report(target: str, project: str | None = None, *,
                reader: Any = None, now: float | None = None) -> dict[str, Any]:
    """What this work order's turn is doing RIGHT NOW — `jarvis watch`'s one entry point"""
    from . import db, holds, live
    from . import worker_session as ws

    name, path, wo = find_work_order(target, project)
    reader = live.Reader() if reader is None else reader
    reader.bind(wo.get("session_id") or "")
    store = ProjectStore(path)
    try:
        # The two facts only the record knows, plus the holds
        return reader.snapshot(
            wo_id=wo["id"], project=name,
            turn_in_flight=ws.busy(store, wo["id"]) is not None,
            settled=wo["status"] not in OPEN_STATUSES,
            now=db.now() if now is None else now,
            holds=holds.held(store, wo["id"]),
            write_floor=inspect_config(name).report_write_floor,
        ).as_dict()
    finally:
        store.close()


#: The precedence, carried in the PAYLOAD and not only in this file's prose
PREFIX_AUTHORITY = (
    "the write classification is the authority and the delta is the hypothesis; "
    "`invariants.check_prefix_stable` is the authoritative measurement of prefix "
    "stability"
)

#: What a work order whose turns all predate the ledger reads as
NOT_RECORDED = ("not recorded for this order — its turns ran before the context ledger "
                "landed")
TURN_NOT_RECORDED = ("not recorded for this turn — it ran before the context ledger "
                     "landed, or its measurement failed")


@observability.metered(OBSERVE_CONTEXT, target="wo_id", project="project")
def context_report(wo_id: str, project: str | None = None, *,
                   turn: int | None = None) -> dict[str, Any]:
    """What Jarvis put in each of a work order's context windows, and the delta (docs/superpowers/specs/2026-09-24-order-observability.md)"""
    from . import autopsy, inspection
    from . import context as context_mod
    from . import usage as usage_mod

    name, path, wo = find_work_order(wo_id, project)
    store = ProjectStore(path)
    try:
        # `all_turns` and not `list_turns`: that one stops at 100 by default
        rows = store.all_turns(wo_id)
        session = wo.get("session_id") or ""
        cfg = inspect_config(name)
        # Spec 2026-09-27 §3: the same rows this report already read
        anatomy, provenance = autopsy.anatomy_for(
            wo, cfg, spans=[], index=usage_mod.index_sessions(),
            turn_starts=[(int(r["seq"]), float(r["started_at"])) for r in rows],
            cold_prefix_floor=cold_prefix_floor())
        if turn is not None and not any(r["seq"] == turn for r in rows):
            raise OpsError(f"{wo_id} has no turn {turn} "
                           f"(it has {len(rows)}: "
                           f"{', '.join(str(r['seq']) for r in rows) or 'none'})")
        out: list[dict[str, Any]] = []
        previous: tuple[int, list[dict[str, Any]]] | None = None
        for row in rows:
            payload = db.from_json(row["context_json"]) if row["context_json"] else None
            entry: dict[str, Any] = {
                "seq": row["seq"], "kind": row["kind"],
                "started_at": row["started_at"], "ended_at": row["ended_at"],
                "recorded": payload is not None,
                # Two different absences, two different keys: `note` is "this turn was never measured"
                "note": "" if payload else TURN_NOT_RECORDED,
                "residual_note": "",
                "ingredients": (payload or {}).get("ingredients") or [],
                "caps": (payload or {}).get("caps") or {},
                "token_bytes": (payload or {}).get("token_bytes"),
                "residual": None, "delta": None, "prefix_break": None,
            }
            if payload:
                entry["residual"] = _turn_residual(entry, anatomy, context_mod,
                                                   inspection)
                if previous is not None:
                    entry["delta"] = context_mod.delta(
                        entry["ingredients"], previous[1], against_seq=previous[0])
                entry["prefix_break"] = _turn_prefix_break(entry, anatomy, inspection)
                previous = (row["seq"], entry["ingredients"])
            out.append(entry)
    finally:
        store.close()

    recorded = [t for t in out if t["recorded"]]
    return {
        "wo_id": wo["id"], "project": name, "title": wo["title"],
        "recorded": bool(recorded),
        # Which reading the residual and the prefix joins above came from (§4)
        "provenance": provenance,
        "note": "" if recorded else NOT_RECORDED,
        "turns": [t for t in out if turn is None or t["seq"] == turn],
    }


def _in_window(ts: float, turn: dict[str, Any]) -> bool:
    """Is this cache write inside the turn's process window?"""
    if ts < (turn["started_at"] or 0.0):
        return False
    end = turn["ended_at"]
    return True if end is None else ts <= end


def _turn_residual(entry: dict[str, Any], anatomy: Any, context_mod: Any,
                   inspection: Any) -> dict[str, Any] | None:
    """The inferred row for one turn, or None with a sentence when it cannot be read"""
    if anatomy is None or not anatomy.found:
        entry["residual_note"] = (
            "Claude Code's own share cannot be inferred: no transcript for this session "
            "— it has expired, or the session never wrote one")
        return None
    cold = next((w for w in anatomy.writes
                 if w.cause == inspection.COLD_START and _in_window(w.ts, entry)), None)
    return context_mod.residual(context_mod.measured_tokens(entry["ingredients"]), cold)


def _turn_prefix_break(entry: dict[str, Any], anatomy: Any,
                       inspection: Any) -> dict[str, Any] | None:
    """The `prefix-miss` writes that land in this turn, joined to its delta"""
    if anatomy is None or not anatomy.found:
        return None
    writes = [w for w in anatomy.writes
              if w.cause == inspection.PREFIX_MISS and _in_window(w.ts, entry)]
    if not writes:
        return None
    delta = entry["delta"]
    grew = (delta or {}).get("biggest_growth")
    if delta is None:
        cause = (f"the prefix broke at turn {entry['seq']} and there is no earlier "
                 f"recorded turn to compare its ingredients against")
    elif grew:
        row = next(r for r in delta["rows"] if r["name"] == grew)
        cause = (f"the prefix broke at turn {entry['seq']} and {grew} grew "
                 f"{row['bytes_delta']:,} bytes since turn {delta['against_seq']}")
    else:
        moved = ", ".join([f"{n} appeared" for n in delta["appeared"]]
                          + [f"{n} disappeared" for n in delta["disappeared"]])
        cause = (f"the prefix broke at turn {entry['seq']} and no measured ingredient "
                 f"grew since turn {delta['against_seq']}"
                 + (f" ({moved})" if moved else ""))
    return {"writes": [w.as_dict() for w in writes],
            "tokens_rewritten": sum(w.written for w in writes),
            "delta": delta, "cause": cause, "authority": PREFIX_AUTHORITY,
            "note": "this report names a cause and raises no alarm of its own"}


def _alarm_dict(name: str, row: dict[str, Any]) -> dict[str, Any]:
    """The twenty keys `list_cost_alarms` publishes, from one `alarms_across` row (docs/superpowers/specs/2026-08-31-the-supervisor.md)"""
    feature = row.get("subject_kind") == "feature_order"
    return {
        "project": name,
        "wo_id": row["wo_id"],
        "title": (row["fo_title"] if feature else row["title"]),
        "status": (row["fo_status"] if feature else row["status"]),
        "hidden": bool(row["hidden"]),
        "ts": row["ts"],
        "kind": row["kind"],
        "seq": row["seq"],
        "reason": row["reason"],
        "live": bool(row["needs_attention"]),
        "id": row["id"],
        "alarm_status": row["alarm_status"],
        "verdict": row["verdict"],
        "note": row["note"],
        "review_status": row["review_status"],
        "neo_question_id": row["neo_question_id"],
        "source": row["source"],
        "probe": row["probe"],
        "subject_kind": row["subject_kind"],
        "subject_id": row["fo_id"] or row["wo_id"],
    }


def list_cost_alarms(project_name: str | None = None, limit: int = 200,
                     wo_id: str | None = None, fo_id: str | None = None,
                     sources: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
    """Every turn the OS raised WHILE it was burning, newest first, across the fleet (docs/superpowers/specs/2026-08-31-the-supervisor.md)"""
    paths = registered_project_paths()
    if project_name:
        if project_name not in paths:
            raise OpsError(f"project {project_name!r} not registered")
        paths = {project_name: paths[project_name]}
    out: list[dict[str, Any]] = []
    for name, path in paths.items():
        if not path.is_dir():
            continue
        store = ProjectStore(path)
        try:
            rows = store.alarms_across(limit=limit, wo_id=wo_id, fo_id=fo_id,
                                       sources=sources)
        finally:
            store.close()
        out.extend(_alarm_dict(name, row) for row in rows)
    out.sort(key=lambda r: r["ts"], reverse=True)
    return out[:limit]


# -- the symptom catalogue: what a project is watched for


def supervisor_probes(project_name: str | None = None,
                      catalog_path: str | None = None) -> list[dict[str, Any]]:
    """The health probes in force, each with WHERE ITS ANSWER CAME FROM (docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md)"""
    from dataclasses import asdict

    catalog = resolve_catalog(catalog_path)
    fleet = {p.id: p for p in catalog.os.supervisor.probes}
    if project_name is None:
        resolved = catalog.os.supervisor.probes
    else:
        resolved = project_spec(catalog, project_name).supervisor.probes

    out: list[dict[str, Any]] = []
    for probe in resolved:
        base = fleet.get(probe.id)
        if base is None:
            source = "project addition"
        elif base != probe:
            source = "project override"
        else:
            source = "fleet"
        row = asdict(probe)
        row["subjects"] = list(probe.subjects)
        row["source"] = source
        out.append(row)
    return out


def probe_titles(catalog_path: str | None = None) -> dict[str, dict[str, str]]:
    """{project: {probe id: title}} for the whole fleet, in ONE catalog load"""
    try:
        catalog = resolve_catalog(catalog_path)
    except (OpsError, CatalogError):
        return {}
    return {spec.name: {p.id: p.title for p in spec.supervisor.probes}
            for spec in catalog.projects}


# -- the review loop: what the supervisor decided, and what the user makes of it


def _reviewable(name: str, row: dict[str, Any],
                answers: dict[int, dict[str, Any]],
                titles: dict[str, dict[str, str]] | None = None,
                results: dict[str, str] | None = None) -> dict[str, Any]:
    """One alarm as a review surface reads it: the frozen dict plus the reasoning"""
    view = _alarm_dict(name, row)
    question = answers.get(row["neo_question_id"]) if row["neo_question_id"] else None
    view.update({
        "verdict_reason": row["verdict_reason"],
        "decided_at": row["decided_at"],
        "attempts": row["attempts"],
        "review_feedback": row["review_feedback"],
        "reviewed_at": row["reviewed_at"],
        "neo_advice": (question or {}).get("answer"),
        "neo_question_status": (question or {}).get("status"),
        # What the supervisor asked to do about it, and what came of it — §6
        "remedy": row.get("remedy"),
        "remedy_argument": row.get("remedy_argument"),
        "remedy_approval_id": row.get("remedy_approval_id"),
        "remedy_result": (results or {}).get(row["id"]),
    })
    # The frozen dict carries the probe ID; every review surface renders its TITLE
    view["kind_label"] = alarm_kind_label(view, titles or {})
    return view


def _neo_answers(question_ids: Sequence[int]) -> dict[int, dict[str, Any]]:
    """Neo's questions by id, in one store open"""
    from .neo_store import NeoStore

    ids = {q for q in question_ids if q}
    if not ids:
        return {}
    neo = NeoStore()
    try:
        found = {qid: neo.get(qid) for qid in ids}
    finally:
        neo.close()
    return {qid: q for qid, q in found.items() if q is not None}


def _remedy_results(store: ProjectStore, rows: list[dict[str, Any]], limit: int,
                    wo_id: str | None = None) -> dict[str, str]:
    """{alarm id: what the remedy actually did}, from the `remedy_applied` events"""
    if not any(r.get("remedy") for r in rows):
        return {}
    out: dict[str, str] = {}
    stream = (list(reversed(store.events_of_kind(wo_id, "remedy_applied"))) if wo_id
              else store.events_across("remedy_applied", limit=max(limit, 200)))
    for event in stream:
        payload = db.from_json(event.get("payload"), {}) or {}
        alarm_id = str(payload.get("alarm_id") or "")
        # Newest first, so the first one wins
        if alarm_id and alarm_id not in out:
            out[alarm_id] = str(payload.get("result") or "")
    return out


def _enriched_alarms(project_name: str | None, limit: int,
                     read: Callable[[ProjectStore], list[dict[str, Any]]]
                     ) -> list[dict[str, Any]]:
    """`read` over every registered project, through `_reviewable`"""
    paths = registered_project_paths()
    if project_name:
        if project_name not in paths:
            raise OpsError(f"project {project_name!r} not registered")
        paths = {project_name: paths[project_name]}
    rows: list[tuple[str, dict[str, Any]]] = []
    results: dict[str, str] = {}
    for name, path in paths.items():
        if not path.is_dir():
            continue
        store = ProjectStore(path)
        try:
            found = read(store)
            results.update(_remedy_results(store, found, limit))
        finally:
            store.close()
        rows.extend((name, r) for r in found)
    answers = _neo_answers([r["neo_question_id"] for _, r in rows])
    titles = probe_titles()
    return [_reviewable(name, row, answers, titles, results) for name, row in rows]


def alarm_feed(project_name: str | None = None, limit: int = 200,
               wo_id: str | None = None, fo_id: str | None = None,
               sources: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
    """`list_cost_alarms`' rows as a REVIEW surface reads them, newest first"""
    out = _enriched_alarms(project_name, limit, lambda store: store.alarms_across(
        limit=limit, wo_id=wo_id, fo_id=fo_id, sources=sources))
    out.sort(key=lambda r: r["ts"], reverse=True)
    return out[:limit]


def alarm_review_queue(project_name: str | None = None, limit: int = 200
                       ) -> list[dict[str, Any]]:
    """Alarms the supervisor answered and the user has not yet looked at, newest first"""
    out = _enriched_alarms(project_name, limit, lambda store: [
        r for r in store.alarms_across(limit=limit, statuses=("acked",))
        if r["review_status"] == "unreviewed"])
    out.sort(key=lambda r: r["decided_at"] or r["ts"], reverse=True)
    return out[:limit]


def _find_alarm(alarm_id: str, project_name: str | None = None
                ) -> tuple[str, Path, dict[str, Any]]:
    """Locate one alarm across the fleet, shaped as `alarms_across` returns it"""
    paths = registered_project_paths()
    if project_name and project_name not in paths:
        raise OpsError(f"project {project_name!r} not registered "
                       f"(known: {sorted(paths)})")
    candidates = {project_name: paths[project_name]} if project_name else paths
    for name, path in candidates.items():
        if not path.is_dir():
            continue
        store = ProjectStore(path)
        try:
            try:
                bare = store.get_alarm(alarm_id)
            except KeyError:
                continue
            joined = [r for r in store.alarms_across(wo_id=bare["wo_id"], limit=1000)
                      if r["id"] == alarm_id]
        finally:
            store.close()
        if joined:
            return name, path, joined[0]
    raise OpsError(f"alarm {alarm_id!r} not found in any registered project")


def alarm_detail(alarm_id: str, project_name: str | None = None) -> dict[str, Any]:
    """One alarm in full — what fired, what the supervisor made of it, what Neo said"""
    name, path, row = _find_alarm(alarm_id, project_name)
    store = ProjectStore(path)
    try:
        results = _remedy_results(store, [row], 200, wo_id=row["wo_id"])
        gate = (store.get_approval(int(row["remedy_approval_id"]))
                if row.get("remedy_approval_id") else None)
    finally:
        store.close()
    view = _reviewable(name, row, _neo_answers([row["neo_question_id"]]),
                       probe_titles(), results)
    view["gate"] = ({k: gate[k] for k in ("id", "kind", "status", "escalated",
                                          "decided_by", "decision_reason",
                                          "justification", "evidence")}
                    if gate else None)
    return view


def review_alarm(alarm_id: str, approved: bool, feedback: str = "",
                 project_name: str | None = None) -> dict[str, Any]:
    """The user's verdict on the supervisor's verdict"""
    from . import supervisor
    from .neo_store import SUPERVISOR_SEAT, NeoStore

    # Every refusal ahead of the first write
    if not approved and not feedback.strip():
        raise OpsError("a correction needs feedback — what should the supervisor "
                       "have decided?")
    name, path, row = _find_alarm(alarm_id, project_name)
    # `failed` is reviewable because nothing re-queues it and it can hold a merge (#1063)
    if row["alarm_status"] not in ALARM_REVIEWABLE_STATUSES:
        # A REFUSAL THAT DOES NOT SAY WHERE TO GO IS A BUG REPORT
        where = ""
        if row["alarm_status"] == "proposed":
            gate = row["remedy_approval_id"] or "<id>"
            where = (f" — it is asking permission to {row['remedy'] or 'act'}, so "
                     f"answer it with `jarvis gate approve {gate}` or "
                     f"`jarvis gate deny {gate} --reason \"…\"`")
        raise OpsError(f"alarm {alarm_id} is {row['alarm_status']}, and only an alarm "
                       f"the supervisor has decided or could not decide "
                       f"({', '.join(repr(s) for s in ALARM_REVIEWABLE_STATUSES)}) can "
                       f"be reviewed{where}")
    review = "approved" if approved else "corrected"
    store = ProjectStore(path)
    try:
        store.update_alarm(alarm_id, review_status=review,
                           review_feedback=feedback.strip(), reviewed_at=db.now())
    finally:
        store.close()

    closed = False
    if not approved or row["neo_question_id"]:
        neo = NeoStore()
        try:
            # THE CORRECTION IS THE MEMORY, AND IT LIVES IN neo.db RATHER THAN ON THE ROW: the alarm is this...
            if not approved:
                neo.add_learning(
                    supervisor.learning_from_review(row, feedback.strip()),
                    project=name, source="review", seat=SUPERVISOR_SEAT,
                    wo_id=row["wo_id"] or "", origin_kind="alarm",
                    origin_id=alarm_id)

            # THE CLOSE SITE NOBODY ELSE OWNS
            if row["neo_question_id"]:
                closed = neo.supersede(
                    row["neo_question_id"],
                    f"The user {review} the supervisor's verdict on alarm {alarm_id}."
                    + (f" Their correction: {feedback.strip()}"
                       if feedback.strip() else ""),
                    reason=f"decided by the user on {alarm_id} itself",
                )
        finally:
            neo.close()
    return {"alarm_id": alarm_id, "project": name, "wo_id": row["wo_id"],
            "review": review, "neo_question_closed": closed}


def _os_calls_detail(wo_id: str, limit: int = 200) -> list[dict[str, Any]]:
    """Every OS call made for one work order, newest first, priced at list"""
    from . import agent_usage
    from . import usage as usage_mod

    central = CentralStore()
    try:
        rows = central.agent_calls(wo_id=wo_id, limit=limit)
    finally:
        central.close()
    out = []
    for row in rows:
        # Subprocesses go to `_subproc_detail`
        if agent_usage.is_subprocess(row["kind"]) or agent_usage.is_observability(
                row["kind"]):
            continue
        u = usage_mod.priced(row["model"] or "unknown", input=row["input"],
                             cache_write=row["cache_write"],
                             cache_read=row["cache_read"], output=row["output"])
        envelope = db.from_json(row.get("usage_json"), {}) or {}
        out.append({
            "ts": row["ts"], "kind": row["kind"],
            "label": row["label"] or agent_usage.describe(row["kind"]),
            "model": row["model"], "ok": bool(row["ok"]),
            "question_id": row["question_id"],
            "cost_usd": row["cost_usd"], "list_cost_usd": round(u.list_cost_usd, 4),
            "input": row["input"], "cache_write": row["cache_write"],
            "cache_read": row["cache_read"], "output": row["output"],
            "billed_input": u.billed_input,
            "api_calls": envelope.get("api_calls"),
            "context_peak": envelope.get("context_peak") or 0,
            # How big the OS's own input to this call was (spec §3 (docs/superpowers/specs/2026-09-26-bounded-model-inputs.md)
            "prompt_chars": row["prompt_chars"],
            "system_prompt_chars": row["system_prompt_chars"],
        })
    return out


def _largest_os_input(wo_id: str) -> dict[str, Any] | None:
    """The biggest OS-side input recorded for one work order, or None if none was (docs/superpowers/specs/2026-09-26-bounded-model-inputs.md)"""
    sized = [r for r in _os_calls_detail(wo_id)
             if (r["prompt_chars"] or 0) + (r["system_prompt_chars"] or 0)]
    if not sized:
        return None
    biggest = max(sized, key=lambda r: r["prompt_chars"] + r["system_prompt_chars"])
    return {"kind": biggest["kind"], "label": biggest["label"],
            "model": biggest["model"], "prompt_chars": biggest["prompt_chars"],
            "system_prompt_chars": biggest["system_prompt_chars"],
            "ts": biggest["ts"]}


def _subproc_detail(groups: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """What the worker spent below itself, by what ran it, dearest first"""
    from . import usage as usage_mod

    out = []
    for g in groups:
        u = _priced_group(usage_mod, g)
        out.append({
            "label": g.get("label") or "claude -p", "model": g.get("model") or "",
            "calls": g.get("calls") or 0, "failed": g.get("failed") or 0,
            "cost_usd": round(g.get("cost_usd") or 0.0, 4),
            "list_cost_usd": round(u.list_cost_usd, 4),
            "billed_input": u.billed_input, "output": u.output,
        })
    return sorted(out, key=lambda g: -g["list_cost_usd"])


# -- what the knowledge base costs, and who actually reads it

#: `MISSED_MIN_WORDS` — how much of a work order's own title has to survive into a search for a title...


def _index_cost(central: CentralStore, name: str, path: Path) -> dict[str, Any]:
    """What the knowledge base costs a dispatch prompt, measured rather than estimated"""
    from .catalog import WorkerDefaults
    from .dispatch import build_worker_prompt

    spec = ProjectSpec(name=name, path=path, worker=WorkerDefaults())
    wo = {"id": "wo-00000000", "title": "measure the index", "description": ""}
    brief = central.knowledge_brief(name)
    whole = len(build_worker_prompt(wo, spec, brief))
    bare = len(build_worker_prompt(wo, spec, None))
    return {
        "project": name,
        "indexed": len(brief.digest), "pinned": len(brief.pinned),
        "overflow": brief.overflow_count, "entries": brief.total,
        "index_chars": whole - bare, "prompt_chars": whole,
        "share_of_prompt": round((whole - bare) / whole, 4) if whole else 0.0,
        "body_chars": central.knowledge_body_chars(name),
    }


#: The side-effect kinds a packet can carry (#200)
SIDE_EFFECT_EVENTS = {
    "knowledge_added": "knowledge_added",
    "knowledge_retracted": "knowledge_retracted",
}


def learn_add(content: str, *, project: str = "", topic: str = "", tags: str = "",
              wo_id: str = "") -> dict[str, Any]:
    """Write a knowledge entry AND record on the work order that it did"""
    from .central_store import headline

    central = CentralStore()
    try:
        row = central.add_knowledge(content, project=project, topic=topic, tags=tags,
                                    wo_id=wo_id)
    finally:
        central.close()
    _record_side_effect(wo_id, "knowledge_added",
                        {"kn_id": row["id"], "topic": topic,
                         "project": project or "global",
                         "headline": headline(content)})
    return row


def learn_retract(knowledge_id: str, reason: str, *, wo_id: str = "") -> dict[str, Any]:
    """Retire a knowledge entry AND record on the work order that it did (#200)"""
    from .central_store import headline

    central = CentralStore()
    try:
        row = central.retract_knowledge(knowledge_id, reason, wo_id=wo_id)
    finally:
        central.close()
    _record_side_effect(wo_id, "knowledge_retracted",
                        {"kn_id": row["id"], "reason": row["retired_reason"],
                         "topic": row["topic"], "headline": headline(row["content"])})
    return row


def _record_side_effect(wo_id: str, kind: str, payload: dict[str, Any]) -> bool:
    """One side-effect event on a work order's timeline"""
    if not wo_id:
        return False  # a person at a terminal, not a worker
    try:
        _name, path, _wo = find_work_order(wo_id)
        store = ProjectStore(path)
        try:
            store.add_event(wo_id, SIDE_EFFECT_EVENTS[kind], payload)
        finally:
            store.close()
    except Exception:  # noqa: BLE001 — see the docstring
        return False
    return True


def _knowledge_effects(_store: ProjectStore, wo_id: str) -> list[dict[str, Any]]:
    """Knowledge-base writes and retractions attributed to this work order (issue #200)"""
    central = CentralStore()
    try:
        rows = central.knowledge_by_work_order(wo_id)
    finally:
        central.close()
    effects = []
    for row in rows:
        if row.get("retired_by_wo_id") == wo_id:
            effects.append({
                "kind": "knowledge_retracted", "id": row["id"],
                "summary": f"retired {row['id']} ({row['topic'] or 'no topic'}): "
                           f"{row['retired_reason'] or '(no reason recorded)'}",
                "detail": row["content"]})
        if row.get("wo_id") == wo_id:
            effects.append({
                "kind": "knowledge_added", "id": row["id"],
                "summary": f"wrote {row['id']} to the {row['project'] or 'global'} "
                           f"knowledge base ({row['topic'] or 'no topic'})",
                "detail": row["content"]})
    return effects


#: What a staged release carries on the work order's timeline
_RELEASE_EVENT_STATES = (("release_verified", "verified"),
                         ("release_restart", "restarting"))


def _release_effects(store: ProjectStore, wo_id: str) -> list[dict[str, Any]]:
    """The release this work order shipped (docs/superpowers/specs/2026-09-17-a-round-with-nothing-to-judge.md)"""
    from . import release

    marker = release.read_marker() or {}
    if str(marker.get("wo_id") or "") == wo_id:
        tag, version = str(marker.get("tag") or ""), str(marker.get("version") or "")
        state, source = str(marker.get("state") or "staged"), "the staged-release marker"
    else:
        tag = version = state = ""
        source = "this work order's timeline"
        for kind, at in _RELEASE_EVENT_STATES:
            events = store.events_of_kind(wo_id, kind)
            if events:
                payload = db.from_json(events[-1]["payload"], {})
                tag = str(payload.get("tag") or "")
                version = str(payload.get("version") or "")
                state = at
                break
    if not tag:
        return []
    unverified = release.verify_release_claim(store, wo_id, version, tag)
    checked = ("production is on this exact version and checked out at this exact tag, "
               "so the release named here really did land"
               if not unverified else
               f"NOT VERIFIED — {unverified}. The claim above comes from "
               f"{source}, which is written state rather than proof, so it is reported "
               f"to you as a claim and judged like any other part of the submission.")
    return [{
        "kind": "release_staged", "id": tag,
        "summary": f"shipped {tag}: release branch and annotated tag pushed to origin, "
                   f"production deployed to that tag and its venv rebuilt "
                   f"(hand-off state: {state})"
                   + ("" if not unverified else " — CLAIMED, NOT VERIFIED"),
        "detail": (
            f"version {version or '?'}, tag {tag}, as recorded by {source}.\n\n"
            f"Cross-checked against the production checkout: {checked}\n\n"
            "What a release does that no diff can show: the release branch and the "
            "annotated tag are pushed to origin, the production checkout is deployed to "
            "that tag and its venv rebuilt, the systemd units are re-rendered, and "
            "`release.verify_on_boot` proves the version on disk and both units' restart "
            "timestamps before settling this work order."),
        # Consumed by the registry, which turns it into `attested`
        "verified": not unverified,
    }]


def _plan_effects(store: ProjectStore, wo_id: str) -> list[dict[str, Any]]:
    """The plan this work order submitted, if it submitted one (docs/superpowers/specs/2026-09-25-a-plan-submission-is-not-an-empty-packet.md §5-)"""
    fo = store.feature_order_for_planner(wo_id) if wo_id else None
    plan = db.from_json(fo.get("plan"), {}) if fo else {}
    if not plan:
        return []
    children = len(plan.get("children") or [])
    return [{
        "kind": "plan_submitted", "id": fo["id"],
        "summary": f"submitted the plan for {fo['id']}: it decomposes into "
                   f"{children} work orders",
        "detail": (
            f"design doc {plan.get('design_doc') or '?'}, {children} children, "
            f"queued for review as Neo question "
            f"{fo.get('plan_question_id') or '?'}.\n\n"
            "What no diff can show: the plan lives in the feature order's `plan` column "
            "and dispatch materialises each child's brief from it."),
        # Strict by §6: only the feature order that points at THIS planner attests it
        "verified": fo.get("plan_wo_id") == wo_id,
    }]


@dataclass(frozen=True)
class SideEffectCollector:
    """One kind of durable change no diff can show, and whether a reviewer can judge it"""

    name: str
    collect: Callable[[ProjectStore, str], list[dict[str, Any]]]
    attested: bool = False


#: THE REGISTRY (#200)
SIDE_EFFECT_COLLECTORS = (
    SideEffectCollector("knowledge", _knowledge_effects),
    SideEffectCollector("release", _release_effects, attested=True),
    SideEffectCollector("plan", _plan_effects, attested=True),
)


def side_effects_of(store: ProjectStore, wo_id: str) -> list[dict[str, Any]]:
    """Durable, non-file change this work order made, for its evidence packet"""
    effects: list[dict[str, Any]] = []
    for collector in SIDE_EFFECT_COLLECTORS:
        for effect in collector.collect(store, wo_id):
            record = {k: v for k, v in effect.items() if k != "verified"}
            record["attested"] = collector.attested and bool(effect.get("verified"))
            effects.append(record)
    return effects


def feature_side_effects(store: ProjectStore, fo_id: str) -> list[dict[str, Any]]:
    """Every child's side effects, in the feature's own child order"""
    effects: list[dict[str, Any]] = []
    for child in store.feature_children(fo_id, kinds=WORKER_CHILDREN):
        for effect in side_effects_of(store, str(child["id"])):
            effects.append({**effect, "wo_id": str(child["id"])})
    return effects


def knowledge_usage_report(project: str | None = None, days: int | None = None,
                           limit: int = 20) -> dict[str, Any]:
    """What memory costs and whether anyone uses it"""
    from .central_store import headline

    since = db.now() - days * 86400 if days else None
    paths = registered_project_paths()
    if project and project not in paths:
        raise OpsError(f"project {project!r} not registered (known: {sorted(paths)})")
    scope = {project: paths[project]} if project else paths

    central = CentralStore()
    try:
        summary = central.knowledge_read_summary(project, since)
        hit_counts = central.knowledge_hit_counts(since)
        by_order = central.knowledge_reads_by_order(since)
        entries = [e for e in central.search_knowledge("", limit=10_000, project=project)
                   if not e.get("retired_at")]
        top: list[dict[str, Any]] = [
            {"id": e["id"], "topic": e["topic"], "reads": hit_counts.get(e["id"], 0),
             "chars": len(e["content"]), "headline": headline(e["content"])}
            for e in entries]
        top.sort(key=lambda e: (-int(e["reads"]), -int(e["chars"])))
        cost = [_index_cost(central, name, path) for name, path in sorted(scope.items())]

        silent: list[dict[str, Any]] = []
        missed: list[dict[str, Any]] = []
        # Nothing before the log's first row was OBSERVED
        observed_from = central.knowledge_log_starts()
        floor = 0.0 if observed_from is None else (
            observed_from if since is None else max(since, observed_from))
        for name, path in sorted(scope.items()):
            store = ProjectStore(path)
            try:
                for wo in store.list_work_orders(statuses=("completed",), limit=500,
                                                 include_hidden=True):
                    if observed_from is None or (wo["created_at"] or 0) < floor:
                        continue
                    if by_order.get(wo["id"]):
                        continue
                    row = {"wo_id": wo["id"], "project": name, "title": wo["title"]}
                    silent.append(row)
                    if len(wo["title"].split()) < MISSED_MIN_WORDS:
                        continue
                    # Only entries that already existed when the order was created: an entry it wrote ITSELF is not...
                    could = [e for e in central.search_knowledge(
                        wo["title"], limit=3, project=name)
                        if not e.get("retired_at") and e["ts"] <= (wo["created_at"] or 0)]
                    if could:
                        missed.append({**row, "entries": [
                            {"id": e["id"], "headline": headline(e["content"])}
                            for e in could]})
            finally:
                store.close()
    finally:
        central.close()

    sizes = sorted(len(e["content"]) for e in entries)
    return {
        "project": project or "", "days": days,
        "entries": len(entries),
        "size": {
            "total_chars": sum(sizes),
            "median_chars": sizes[len(sizes) // 2] if sizes else 0,
            "max_chars": sizes[-1] if sizes else 0,
            # An entry whose first line overflows the headline reaches the index as a sentence cut mid-word
            "truncated_headlines": sum(
                1 for e in entries
                if len(e["content"].split("\n", 1)[0]) > len(headline(e["content"]))),
        },
        "prompt_cost": cost,
        "reads": summary,
        "read_chars_per_order": (round(summary["chars"] / summary["orders"])
                                 if summary["orders"] else 0),
        # When the read log begins
        "observed_from": observed_from,
        "top_entries": top[:limit],
        "never_read": [e for e in top if e["reads"] == 0][:limit],
        "never_read_count": sum(1 for e in top if e["reads"] == 0),
        "silent_orders": silent[:limit], "silent_order_count": len(silent),
        "could_have_read": missed[:limit], "could_have_read_count": len(missed),
    }


def _local_day(ts: float) -> str:
    """The same bucket SQLite's `strftime(..., 'localtime')` produces, in Python"""
    from datetime import datetime

    return datetime.fromtimestamp(ts).strftime("%Y-%m-%d")


#: What `by_kind` cannot say, carried beside it rather than left for a reader to assume (docs/superpowers/specs/2026-10-01-neo-observability.md)
NEO_ASSUMPTION_KIND_NOTE = (
    "`assumption` covers BOTH auto-review passes: which pass filed one is only on the "
    "project store's `autoreview_asked` event, and splitting on it means opening every "
    "project database and walking events per question"
)

#: THE ESCALATION CLASS THESE COUNTS CANNOT SHOW, named rather than left to vanish
NEO_ESCALATION_INVISIBLE_NOTE = (
    "an assumption the OS held because the stakes classifier could not be reached, or "
    "could not be read, never became a Neo question at all — so those are not in these "
    "counts, in any class"
)


#: What the question counts are, said beside them now that they are not the headline (io-51469add)
NEO_QUESTIONS_PLUMBING_NOTE = (
    "PLUMBING, not the rate you feel: one assumption row makes 0, 1 or 2 questions "
    "(none when a net holds it before any model call, two when an early reading is "
    "confirmed), and a superseded question is the OS closing one, not Neo deciding — "
    "the ASSUMPTIONS block above is per row"
)

#: The three buckets every assumption row lands in, the kn-18a6444b recipe's names
ASSUMPTION_BUCKETS = ("neo_decided", "neo_escalated", "never_reached_neo")


def _share(n: int, of: int) -> float | None:
    return round(n / of, 4) if of else None


def assumption_row_outcomes(facts: Sequence[dict[str, Any]],
                            now: float | None = None) -> dict[str, Any]:
    """Neo's assumption rate PER ROW, three buckets, unsettled apart (kn-18a6444b)"""
    now = db.now() if now is None else now
    out: dict[str, Any] = {key: 0 for key in ASSUMPTION_BUCKETS}
    never_by_hold: dict[str, int] = {}
    unsettled = {"count": 0, "reached_neo": 0, "never_reached_neo": 0}
    ages: list[float] = []
    rows = high = alarms = 0
    dropped = {"total": 0, "re_asked": 0}
    for f in facts:
        for key in dropped:
            dropped[key] += int(f["dropped"][key])
        high += len(f["high_stakes"])
        alarms += len(f["false_alarms"] & f["high_stakes"])
        for a in f["rows"]:
            rows += 1
            aid = int(a["id"])
            # Reached = a question was ever filed: the column, or the ask event a cleared link leaves behind
            reached = bool(a.get("neo_question_id") or a.get("confirm_question_id")
                           or aid in f["asked"])
            if str(a.get("status") or "") == "pending":
                unsettled["count"] += 1
                unsettled["reached_neo" if reached else "never_reached_neo"] += 1
                ages.append(max(0.0, now - float(a.get("ts") or now)) / 86400)
                continue
            # The DECIDER column alone, never prose: kn-18a6444b's trap 1
            if str(a.get("decided_by") or "") == "neo":
                out["neo_decided"] += 1
            elif reached:
                out["neo_escalated"] += 1
            else:
                out["never_reached_neo"] += 1
                code = f["last_hold"].get(aid) or "no hold recorded"
                never_by_hold[code] = never_by_hold.get(code, 0) + 1
    settled = sum(out[key] for key in ASSUMPTION_BUCKETS)
    ages.sort()
    unsettled["median_age_days"] = (round(ages[len(ages) // 2], 1) if ages else None)
    unsettled["max_age_days"] = round(ages[-1], 1) if ages else None
    return {
        "rows": rows, "settled": settled, **out,
        "rates": {key: _share(out[key], settled) for key in ASSUMPTION_BUCKETS},
        "never_reached_by_hold": never_by_hold,
        "unsettled": unsettled,
        "dropped_rulings": dropped,
        # The net's own defect rate, the `jarvis gate rules` false-positive idiom
        "stakes_net": {"held": high, "false_alarms": alarms,
                       "false_alarm_rate": _share(alarms, high)},
    }


def objection_outcomes(store: ProjectStore, since: float | None = None) -> dict[str, Any]:
    """Sent, delivered, ACTED ON, withdrawn — `objection_counts` plus `objection_response` (io-ce566253)"""
    counted = store.objection_counts(since=since)
    acted = 0
    for a in store.delivered_objections(since=since):
        if objection_response(store, a) is not None:
            acted += 1
    return {**counted, "acted_on": acted}


def _escalation_rate(answered: int, escalated: int, failed: int) -> float | None:
    """`escalated` ALONE over SETTLED questions, `None` — never `0.0` — with no denominator"""
    settled = answered + escalated + failed
    if not settled:
        return None
    return round(escalated / settled, 4)


def _unreachable_rate(answered: int, escalated: int, failed: int) -> float | None:
    """`_escalation_rate`'s sibling: `failed` over the same settled denominator"""
    settled = answered + escalated + failed
    if not settled:
        return None
    return round(failed / settled, 4)


def _neo_outcome_bucket() -> dict[str, Any]:
    return {"asked": 0, "answered": 0, "escalated": 0, "failed": 0,
            "escalation_rate": None, "unreachable_rate": None}


def _neo_spend_bucket() -> dict[str, Any]:
    return {"calls": 0, "input": 0, "cache_write": 0, "cache_read": 0, "output": 0,
            "recorded_cost_usd": 0.0, "list_cost_usd": 0.0}


def _percentile(sorted_values: list[int], fraction: float) -> int | None:
    """Nearest-rank percentile over a sample, or None when the sample is empty"""
    if not sorted_values:
        return None
    index = min(len(sorted_values) - 1,
                max(0, int(round(fraction * (len(sorted_values) - 1)))))
    return int(sorted_values[index])


def neo_stats_report(project: str | None = None, days: int | None = None,
                     limit: int = 20) -> dict[str, Any]:
    """Neo's volume, outcomes, escalation causes, spend and latency (docs/superpowers/specs/2026-10-01-neo-observability.md)"""
    from . import agent_usage
    from . import usage as usage_mod
    from .neo_store import (ESCALATION_CAUSES_CHOSEN, ESCALATION_CAUSES_FAILED,
                            ESCALATION_CAUSES_OVERRIDDEN, NEO_HELD_Q_STATUSES,
                            NeoStore)

    since = db.now() - days * 86400 if days else None
    paths = registered_project_paths()
    if project and project not in paths:
        raise OpsError(f"project {project!r} not registered (known: {sorted(paths)})")
    scope = {project: paths[project]} if project else paths

    questions = {"asked": 0, "answered": 0, "escalated": 0, "failed": 0, "open": 0,
                 "superseded": 0, "escalation_rate": None, "unreachable_rate": None}
    by_kind: dict[str, dict[str, Any]] = {}
    by_project: dict[str, dict[str, Any]] = {}
    by_day: dict[str, dict[str, Any]] = {}

    neo = NeoStore()
    try:
        outcomes = neo.question_outcomes(project or "", since)
        cause_rows = neo.escalation_cause_counts(project or "", since)
        order_rows = neo.questions_per_order(project or "", since)
    finally:
        neo.close()

    for row in outcomes:
        n = int(row["n"] or 0)
        kind = row["kind"] or "question"
        buckets = [questions,
                   by_kind.setdefault(kind, _neo_outcome_bucket()),
                   by_project.setdefault(row["project"] or "", _neo_outcome_bucket()),
                   by_day.setdefault(row["day"], {**_neo_outcome_bucket(),
                                                  "day": row["day"]})]
        for bucket in buckets:
            bucket["asked"] += n
        if row["status"] == "answered" and row["answered_by"] == "neo":
            for bucket in buckets:
                bucket["answered"] += n
        elif row["status"] == "answered" and row["answered_by"] == "os":
            # `NeoStore.supersede`: decided somewhere else, so neither Neo answering nor Neo handing back
            questions["superseded"] += n
        elif row["status"] in ("escalated", "failed"):
            for bucket in buckets:
                bucket[row["status"]] += n
        if row["status"] in NEO_HELD_Q_STATUSES:
            questions["open"] += n

    for bucket in (questions, *by_kind.values(), *by_project.values(),
                   *by_day.values()):
        bucket["escalation_rate"] = _escalation_rate(
            bucket["answered"], bucket["escalated"], bucket["failed"])
        bucket["unreachable_rate"] = _unreachable_rate(
            bucket["answered"], bucket["escalated"], bucket["failed"])

    # A day inside the window with no questions is a measured zero and appears
    if since is not None:
        for step in range(int(days or 0) + 1):
            day = _local_day(since + step * 86400)
            by_day.setdefault(day, {**_neo_outcome_bucket(), "day": day,
                                    "escalation_rate": None,
                                    "unreachable_rate": None})
    question_days = [by_day[day] for day in sorted(by_day)][-limit:]

    chosen: dict[str, int] = {}
    overridden_causes: dict[str, int] = {}
    failed_causes: dict[str, int] = {}
    not_recorded = 0
    for row in cause_rows:
        cause, n = row["cause"], int(row["n"] or 0)
        if cause in ESCALATION_CAUSES_CHOSEN:
            chosen[cause] = chosen.get(cause, 0) + n
        elif cause in ESCALATION_CAUSES_OVERRIDDEN:
            # The third answer to "who decided": Neo answered and the OS did not take it
            overridden_causes[cause] = overridden_causes.get(cause, 0) + n
        elif cause in ESCALATION_CAUSES_FAILED:
            failed_causes[cause] = failed_causes.get(cause, 0) + n
        else:
            # NULL, '' or a member a later release removed
            not_recorded += n

    central = CentralStore()
    try:
        groups = central.agent_call_totals_by_day(
            project, since, kinds=sorted(agent_usage.NEO_KINDS))
        latency_rows = central.agent_call_latencies(
            project, since, kinds=sorted(agent_usage.NEO_KINDS))
    finally:
        central.close()

    spend_totals = _neo_spend_bucket()
    spend_by_kind: dict[str, dict[str, Any]] = {}
    spend_by_project: dict[str, dict[str, Any]] = {}
    spend_by_day: dict[str, dict[str, Any]] = {}
    for g in groups:
        # Priced PER MODEL GROUP through the same path `cost_report` uses: a digest on Haiku is not Opus...
        priced = _priced_group(usage_mod, g)
        targets = [spend_totals,
                   spend_by_kind.setdefault(g["kind"], _neo_spend_bucket()),
                   spend_by_project.setdefault(g["project"] or "",
                                               _neo_spend_bucket()),
                   spend_by_day.setdefault(g["day"], {**_neo_spend_bucket(),
                                                      "day": g["day"]})]
        for bucket in targets:
            bucket["calls"] += int(g["calls"] or 0)
            for token in ("input", "cache_write", "cache_read", "output"):
                bucket[token] += int(g[token] or 0)
            bucket["recorded_cost_usd"] = round(
                bucket["recorded_cost_usd"] + (g["cost_usd"] or 0.0), 6)
            bucket["list_cost_usd"] = round(
                bucket["list_cost_usd"] + priced.list_cost_usd, 6)

    latency_by_kind: dict[str, dict[str, Any]] = {}
    samples: dict[str, list[int]] = {}
    unmeasured = 0
    for row in latency_rows:
        kind = row["kind"]
        entry = latency_by_kind.setdefault(
            kind, {"calls": 0, "measured": 0, "p50_ms": None, "p90_ms": None,
                   "max_ms": None})
        entry["calls"] += 1
        if row["latency_ms"] is None:
            unmeasured += 1
            continue
        entry["measured"] += 1
        samples.setdefault(kind, []).append(int(row["latency_ms"]))
    for kind, entry in latency_by_kind.items():
        values = sorted(samples.get(kind, []))
        entry["p50_ms"] = _percentile(values, 0.5)
        entry["p90_ms"] = _percentile(values, 0.9)
        entry["max_ms"] = max(values) if values else None

    asked_by_wo = {(row["project"], row["wo_id"]): int(row["n"] or 0)
                   for row in order_rows}
    work_orders = feature_orders = 0
    wo_questions = fo_questions = 0
    # WHAT THE OBJECTIONS DID, beside the escalation rate (docs/superpowers/specs/2026-10-20-an-objection-is-withdrawn-before-the-order-stops.md)
    objections: dict[str, Any] = {"sent": 0, "delivered": 0, "acted_on": 0,
                                  "withdrawn": 0, "by_reason": {}}
    row_facts: list[dict[str, Any]] = []
    for name, path in sorted(scope.items()):
        store = ProjectStore(path)
        try:
            orders = [wo for wo in store.list_work_orders(limit=10_000,
                                                          include_hidden=True)
                      if since is None or (wo["created_at"] or 0) >= since]
            features = [fo for fo in store.list_feature_orders(kind="feature",
                                                               limit=10_000)
                        if since is None or (fo["created_at"] or 0) >= since]
            counted = objection_outcomes(store, since=since)
            row_facts.append(store.assumption_review_facts(since=since))
        finally:
            store.close()
        for key in ("sent", "delivered", "acted_on", "withdrawn"):
            objections[key] += int(counted[key])
        for reason, n in counted["by_reason"].items():
            objections["by_reason"][reason] = (
                objections["by_reason"].get(reason, 0) + int(n))
        # Hidden orders INCLUDED, `cost_report`'s reason: hiding is a gesture about attention
        work_orders += len(orders)
        feature_orders += len(features)
        feature_ids = {fo["id"] for fo in features}
        # A feature order's questions are its planner's and its children's
        family = {fo["plan_wo_id"] for fo in features if fo.get("plan_wo_id")}
        for wo in orders:
            asked = asked_by_wo.get((name, wo["id"]), 0)
            wo_questions += asked
            if wo.get("parent_id") in feature_ids or wo["id"] in family:
                fo_questions += asked
    # The same three figures under names a renderer can read: `by_reason` is keyed by the reason TEXT
    objections["stopped"] = int(
        objections["by_reason"].get(OBJECTION_WITHDRAWN_REASON, 0))
    objections["handed_back"] = int(
        objections["by_reason"].get(OBJECTION_HANDED_BACK_REASON, 0))
    objections["other"] = int(objections["by_reason"].get("other", 0))
    objections["withdrawal_rate"] = _share(objections["withdrawn"], objections["sent"])
    per_order = {
        "work_orders": work_orders,
        "questions_per_wo": (round(wo_questions / work_orders, 2)
                             if work_orders else None),
        "feature_orders": feature_orders,
        "questions_per_fo": (round(fo_questions / feature_orders, 2)
                             if feature_orders else None),
    }

    return {
        "scope": project or "fleet", "days": days, "since": since,
        # THE HEADLINE: per assumption ROW, what the user's inbox actually shows (io-51469add)
        "assumptions": assumption_row_outcomes(row_facts),
        "questions": questions, "questions_note": NEO_QUESTIONS_PLUMBING_NOTE,
        "objections": objections,
        "by_kind": by_kind, "by_kind_note": NEO_ASSUMPTION_KIND_NOTE,
        "by_project": by_project,
        "by_day": question_days,
        "causes": {"chosen": chosen, "overridden": overridden_causes,
                   "failed": failed_causes, "not_recorded": not_recorded},
        "causes_note": NEO_ESCALATION_INVISIBLE_NOTE,
        "per_order": per_order,
        "spend": {"totals": spend_totals, "by_kind": spend_by_kind,
                  "by_project": spend_by_project,
                  "by_day": [spend_by_day[day] for day in sorted(spend_by_day)][-limit:]},
        "latency": {"by_kind": latency_by_kind, "unmeasured": unmeasured},
        "floor": True, "floor_reason": COST_FLOOR_NOTE,
    }


# -- budgets


def work_order_budget(wo_id: str, project_name: str | None = None) -> dict[str, Any]:
    """What this work order's ceiling is and what it has spent"""
    name, path, wo = find_work_order(wo_id, project_name)
    store = ProjectStore(path)
    central = CentralStore()
    try:
        cap = budget.ceiling(store, central, wo)
        spend = budget.spent(store, central, wo_id)
        # The turn in flight, which the enforcement's two queries cannot see and the reader can (issue #471
        live = budget.in_flight(store, wo_id)
        parent = _parent_of(store, wo)
        parent_pool = budget.pool(store, central, parent) if parent else None
        # WHAT THE FAMILY IS CALLED, for the surfaces that render these numbers
        family = family_prose(parent["kind"])[0] if parent else None
        # THE SAME SENTENCE THE SET PATH RETURNS (§1 of... (docs/superpowers/specs/2026-10-01-a-family-capped-raise-must-say-so.md)
        note = ""
        if cap is not None and cap.source == "feature" and cap.exhausted:
            note = _still_capped_note(cap, spend.total_usd, parent,
                                      _feature_unreserved(store, central, wo))
    finally:
        central.close()
        store.close()
    return {
        "project": name, "wo_id": wo_id, "title": wo["title"], "status": wo["status"],
        "budget_usd": wo.get("budget_usd"),
        "reserved_usd": wo.get("budget_reserved_usd"),
        "worker_usd": spend.worker_usd, "jarvis_usd": spend.jarvis_usd,
        "spent_usd": spend.total_usd,
        # RECORDED plus IN FLIGHT, and the two are kept apart rather than merged: one is the CLI's own figure...
        "in_flight_usd": live,
        "live_spent_usd": spend.total_usd + live,
        "cap_usd": cap.cap_usd if cap else None,
        "cap_source": cap.source if cap else None,
        "remaining_usd": cap.remaining_usd if cap else None,
        "live_remaining_usd": (cap.cap_usd - spend.total_usd - live) if cap else None,
        "feature_unreserved_usd": parent_pool.unreserved_usd if parent_pool else None,
        "family": family,
        "note": note,
    }


def set_work_order_budget(wo_id: str, amount: float | None,
                          project_name: str | None = None) -> dict[str, Any]:
    """Set, change or clear a work order's budget — and resume it if it had stopped"""
    name, path, wo = find_work_order(wo_id, project_name)
    if wo["status"] in TERMINAL_STATUSES:
        raise OpsError(
            f"{wo_id} is {wo['status']} — a settled order has nothing left to spend")
    store = ProjectStore(path)
    central = CentralStore()
    resumed = False
    note = ""
    try:
        store.update_work_order(wo_id, budget_usd=amount)
        store.add_event(wo_id, "budget_set", {
            "budget_usd": amount, "previous_usd": wo.get("budget_usd"), "by": "user"})
        fresh = store.get_work_order(wo_id)
        if fresh["status"] == budget.EXHAUSTED and fresh.get("budget_reserved_usd"):
            budget.reserve(store, central, fresh)
            fresh = store.get_work_order(wo_id)
        spend = budget.spent(store, central, wo_id)
        if fresh["status"] == budget.EXHAUSTED:
            resumed, note = resume_raised_budget(store, central, name, fresh,
                                                 fleet.load_pause(central))
    finally:
        central.close()
        store.close()
    return {"project": name, "wo_id": wo_id, "title": wo["title"],
            "budget_usd": amount, "previous_usd": wo.get("budget_usd"),
            "spent_usd": spend.total_usd, "resumed": resumed, "note": note}


#: A raised budget's relaunch the user's pause held back (#1130)
BUDGET_RESUME_HELD = "budget_resume_held"
#: That held relaunch has been let go: the pause lifted or the order was allow-listed
BUDGET_RESUME_RELEASED = "budget_resume_released"


def budget_resume_outstanding(store: ProjectStore, wo_id: str) -> bool:
    """Is a raised budget's relaunch still waiting on the pause"""
    held = store.last_event_of_kind(wo_id, BUDGET_RESUME_HELD)
    if held is None:
        return False
    later = (store.last_event_of_kind(wo_id, k)
             for k in ("budget_exhausted", BUDGET_RESUME_RELEASED))
    return not any(e is not None and e["ts"] >= held["ts"] for e in later)


def budget_held_note(wo_id: str) -> str:
    """Why a raised budget did not relaunch: the fleet is paused (#1130)"""
    return (f"budget raised, but held by the fleet pause — `jarvis resume {wo_id}` "
            "lets it through, and it resumes on its own once the pause lifts")


def resume_raised_budget(store: ProjectStore, central: CentralStore, project_name: str,
                         wo: dict[str, Any],
                         pause: fleet.FleetPause | None) -> tuple[bool, str]:
    """Relaunch a parked order whose budget now has room — unless the user's pause holds it (#1130)"""
    cap = budget.ceiling(store, central, wo)
    if cap is not None and cap.exhausted:
        return False, _still_capped_note(
            cap, budget.spent(store, central, wo["id"]).total_usd, _parent_of(store, wo),
            _feature_unreserved(store, central, wo))
    refused = budget.launch_refusal(store, central, wo)
    if refused is not None:
        # Raised, but by less than one call: the next turn would be refused, so it stays parked and says why
        store.flag_attention(wo["id"], refused.reason)
        return False, refused.reason
    if pause is not None and not pause.allows(wo["id"]):
        note = budget_held_note(wo["id"])
        store.flag_attention(wo["id"], note)
        store.add_event(wo["id"], BUDGET_RESUME_HELD, {"cause": "fleet_paused"})
        return False, note
    return _resume_after_budget(store, project_name, wo)


def _still_capped_note(cap: budget.Ceiling, spent_usd: float,
                       parent: dict[str, Any] | None,
                       unreserved_usd: float) -> str:
    """Why a raised budget changed nothing — ONE WORDING, for the set path and the show path (docs/superpowers/specs/2026-10-01-a-family-capped-raise-must-say-so.md)"""
    spent, cap_usd = budget.format_usd(spent_usd), budget.format_usd(cap.cap_usd)
    if cap.source != "feature":
        return f"still over its ceiling — {spent} spent against {cap_usd}"
    family, verb = family_prose((parent or {}).get("kind"))
    # `format_money`, not `format_usd`: the remainder being NOTHING is the entire condition for this...
    return (f"still over its ceiling: {spent} spent against {cap_usd}, which is its "
            f"{family}'s slice. The {family} has "
            f"{budget.format_money(unreserved_usd)} unreserved of "
            f"its {budget.format_usd((parent or {}).get('budget_usd'))} budget, so "
            f"raise the {family} with `{verb}`.")


def _parent_of(store: ProjectStore, wo: dict[str, Any]) -> dict[str, Any] | None:
    """The feature-order row this work order belongs to, or None"""
    if not wo.get("parent_id"):
        return None
    try:
        return store.get_feature_order(wo["parent_id"])
    except KeyError:
        return None


def _feature_unreserved(store: ProjectStore, central: CentralStore,
                        wo: dict[str, Any]) -> float:
    """What this child's feature has that no live child has claimed. 0.0 if it has none"""
    parent_id = wo.get("parent_id")
    if not parent_id:
        return 0.0
    try:
        fo = store.get_feature_order(parent_id)
    except KeyError:
        return 0.0
    p = budget.pool(store, central, fo, claimant=wo["id"])
    return p.unreserved_usd if p else 0.0


def _resume_after_budget(store: ProjectStore, project_name: str,
                         wo: dict[str, Any]) -> tuple[bool, str]:
    """Put a topped-up work order back to work, or say why it could not be"""
    from . import worker_session

    turn = store.latest_turn(wo["id"])
    if turn is None:
        # Never dispatched — it stopped before its first turn ran
        store.set_status(wo["id"], "pending")
        store.clear_attention(wo["id"])
        return True, "back on the dispatch queue"
    try:
        catalog = resolve_catalog()
        spec = project_spec(catalog, project_name)
    except OpsError as e:
        store.set_status(wo["id"], "pending")
        store.clear_attention(wo["id"])
        return False, f"budget raised, but the catalog could not be read ({e})"
    pause = worker_session.TurnPause(
        # Constructed, never diagnosed: `turn_pause` returns None for a budget stop (worker_session._reap...
        reason=worker_session.PAUSE_BUDGET, turn=turn, retry_at=0.0, attempts=1,
        message="its budget was raised",
    )
    store.set_status(wo["id"], "running")
    store.clear_attention(wo["id"])
    queued = store.queued_messages(wo["id"])
    # A RELAUNCH THIS ORDER IS ALREADY HOLDING IS THE RESUME
    if any(m["source"] == worker_session.RESUME_SOURCE for m in queued):
        return True, "its queued relaunch goes out on the next tick"
    # DECIDED BY THE TURN'S STATE, NOT ITS KIND: only a FAILED turn is relaunched (io-12b7d873)
    if turn["state"] != "failed":
        if queued:
            return True, "its queued message goes out on the next tick"
        return True, (f"nothing to relaunch — turn {turn['seq']} had finished; it "
                      "settles from its record on the next tick")
    try:
        turn_row = worker_session.compact_before_relaunch(
            store, spec, wo, catalog.os.compact_min_context, pause=pause)
        fresh = turn_row or worker_session.retry(store, spec, wo, pause)
        if fresh is None:
            return True, (f"turn {turn['seq']}'s compaction had landed — "
                          + ("its queued message goes out on the next tick" if queued
                             else "nothing left to relaunch"))
    except budget.BudgetExhausted as e:
        budget.escalate(store, store.get_work_order(wo["id"]), e.exhausted)
        return False, "still has no headroom — it stopped again immediately"
    except Exception as e:  # noqa: BLE001 — see the docstring
        store.set_status(wo["id"], "pending")
        return False, f"budget raised, but the relaunch failed ({e})"
    store.add_event(wo["id"], "budget_resumed",
                    # The seq is the COMPACTION's when one won
                    {"budget_usd": wo.get("budget_usd"), "turn": fresh["seq"],
                     "compacted": turn_row is not None})
    return True, f"resumed at turn {fresh['seq']}"


def set_feature_budget(fo_id: str, amount: float | None,
                       project_name: str | None = None) -> dict[str, Any]:
    """Set, change or clear a FEATURE order's family budget"""
    name, path, fo = find_feature_order(fo_id, project_name)
    store = ProjectStore(path)
    central = CentralStore()
    try:
        store.update_feature_order(fo_id, budget_usd=amount)
        p = budget.pool(store, central, store.get_feature_order(fo_id))
        # `budget.family`, NOT `store.feature_children`: the budget is enforced over the family (docs/superpowers/specs/2026-10-01-a-family-capped-raise-must-say-so.md)
        stuck = [c["id"] for c in budget.family(store, store.get_feature_order(fo_id))
                 if c["status"] == budget.EXHAUSTED]
    finally:
        central.close()
        store.close()
    return {"project": name, "fo_id": fo_id, "title": fo["title"],
            "budget_usd": amount, "previous_usd": fo.get("budget_usd"),
            "spent_usd": p.spent_usd if p else None,
            "unreserved_usd": p.unreserved_usd if p else None,
            "exhausted_children": stuck}


# -- who is going to act on this work order

@dataclass(frozen=True)
class Actor:
    """The daemon pass that will act on one work order next"""

    pass_name: str   # the daemon method, exactly as spelled
    why: str         # one sentence naming the predicate that said yes
    predicate: str   # dotted path of the IMPORTED symbol, e.g. "daemon.retries_turn"


@dataclass(frozen=True)
class _Pass:
    """One candidate actor: the pass, its sentence, and the predicate that decides"""

    pass_name: str
    # A CALLABLE when the sentence depends on the row
    why: str | Callable[..., str]
    predicate: str
    test: Callable[..., Any]


@dataclass(frozen=True)
class ActorPasses:
    """Every candidate for one status, or the reason there is deliberately none"""

    passes: tuple[_Pass, ...] = ()
    none_because: str = ""


def _pending_why(store: ProjectStore, wo: dict[str, Any],
                 now: float | None = None) -> str:
    """`pending`'s sentence, composed from the claim SQL's own reader"""
    delay = store.why_not_claimable(wo)
    if delay is None:
        return "`Daemon.dispatch_pending`'s claim SQL would take this row now"
    return f"`Daemon.dispatch_pending` owns this row and its turn is later: {delay}"


#: `waiting_pr_merge`'s sentence when nothing more specific is on record
PR_POLL_WHY = ("`daemon.polls_and_can_act_on_pull_request` says yes: the row carries a "
               "routing pull request and GitHub has not stopped recomputing it, so the "
               "poll asks what became of it and some branch of it can move the row")


def _pr_poll_why(store: ProjectStore, wo: dict[str, Any],
                 now: float | None = None) -> str:
    """`waiting_pr_merge`'s sentence, naming cancelled CI as cancelled (#1123)"""
    from . import automerge, ci_rerun

    head = str(wo.get("pr_head_oid") or "")
    if not head:
        return PR_POLL_WHY
    ep = ci_rerun.episode(store, wo["id"], head)
    if ep.stuck is not None and str(wo.get("pr_checks_stuck_head") or "") == head:
        return (f"the poll still reads the pull request, but CI on {head[:10]} was "
                f"cancelled and the OS has stopped re-running it — "
                f"{ep.stuck.get('blocker') or ''}")
    if ep.reruns:
        last = ep.reruns[-1]
        runs = ", ".join(str(i) for i in last.get("run_ids") or []) or "unrecorded"
        return (f"CI on {head[:10]} was cancelled ({last.get('checks') or 'a check'}), "
                f"not failed; the OS re-ran it once (run {runs}) and the poll waits for "
                f"that run")
    held = store.events_of_kind(wo["id"], "automerge_held")
    payload = db.from_json(held[-1]["payload"], {}) if held else {}
    if (str(payload.get("code") or "") == automerge.HELD_CHECKS_CANCELLED
            and str(payload.get("head_sha") or "") == head):
        return (f"CI on {head[:10]} was cancelled, not failed — "
                f"{payload.get('reason') or ''}; the poll re-runs it once on its next "
                f"tick")
    return PR_POLL_WHY


#: ONE expression of one rule, registered under EVERY open status: `gates.sweep_unargued` has no...
REVIEWS_GATE = _Pass(
    "Daemon._neo_drain",
    "a privileged-action request is open and the user does not hold it. "
    "`pass_name` holds one string and the predicate is the OR of two passes: "
    "`Daemon._neo_drain` settles a pending non-escalated approval, and "
    "`Daemon.abandon_unargued_gates` (via `gates.sweep_unargued`) closes a "
    "held `awaiting_case` one on a timer (Neo 1570). Neither filters on status, and "
    "filing an `awaiting_case` request does not park the row, so the request outlives "
    "whatever status it was filed under",
    "daemon.reviews_gate", reviews_gate)

#: Shared for REVIEWS_GATE's reason: the panel's sweep filters on `OPEN_STATUSES` (issue #1199)
JUDGES_ROUND = _Pass(
    "Daemon.validation_tick",
    "the panel owns a round on this row, so it judges it — a validation "
    "hold only DELAYS that turn (`daemon.runnable_round` is the pass's own "
    "now-or-not test; §2: None means nobody will act, never not yet). "
    "`Daemon.validation_tick` sweeps `work_orders_awaiting_validation`, which "
    "filters on `OPEN_STATUSES` and not on `validating`: since issue 212 a round "
    "runs in PARALLEL with the status the delivery left behind",
    "daemon.validation_round", validation_round)

#: Keyed in `OPEN_STATUSES` order
SCHEDULED_ACTORS: dict[str, ActorPasses] = {
    "pending": ActorPasses((
        _Pass("Daemon.dispatch_pending", _pending_why,
              "project_store.claim_sql_selects", claim_sql_selects),
        JUDGES_ROUND,
        REVIEWS_GATE,
    )),
    "dispatching": ActorPasses((
        _Pass("Daemon.settle_turns",
              "the claim is inside its grace or a turn is already on record, so the "
              "settler will move this row",
              "daemon.settles_turn", settles_turn),
        JUDGES_ROUND,
        REVIEWS_GATE,
    )),
    "running": ActorPasses((
        _Pass("Daemon.settle_turns",
              "the settler sweeps this status and a turn is on record (or the claim "
              "is still inside its grace)",
              "daemon.settles_turn", settles_turn),
        _Pass("Daemon.retry_paused_turns",
              "the turn is in a resumable pause that is due, so the sweep relaunches "
              "it",
              "daemon.retries_turn", retries_turn),
        _Pass("Daemon.deliver_messages",
              "a queued message is deliverable, so it goes out as the next turn",
              "daemon.delivers_message", delivers_message),
        JUDGES_ROUND,
        REVIEWS_GATE,
    )),
    "idle": ActorPasses((
        _Pass("Daemon.deliver_envelopes",
              "an idle manager's designed steady state is a POSITIVE actor: this row "
              "IS a manager and its feature is live, so the bus still routes to it — "
              "including `bus.ChildrenLanded` from `Daemon.settle_features` via "
              "`ops.revive_feature_manager`",
              "ops.manager_awaiting_feature", manager_awaiting_feature),
        JUDGES_ROUND,
        REVIEWS_GATE,
    )),
    "waiting_input": ActorPasses((
        _Pass("Daemon.retry_paused_turns",
              "the auth pause is resumable and due, so the sweep relaunches the turn",
              "daemon.retries_turn", retries_turn),
        _Pass("Daemon.deliver_messages",
              "a queued message is deliverable, so it un-parks this row",
              "daemon.delivers_message", delivers_message),
        _Pass("Daemon._neo_drain",
              "Neo still holds the question this order is parked on",
              "daemon.answers_question", answers_question),
        JUDGES_ROUND,
        REVIEWS_GATE,
    )),
    "validating": ActorPasses((
        JUDGES_ROUND,
        _Pass("Daemon.poll_pull_requests",
              "the row carries a routing pull request, so the poll asks GitHub what "
              "became of it",
              "daemon.polls_pull_request", polls_pull_request),
        REVIEWS_GATE,
    )),
    # The USER is the actor: `true_blockers`' four-way triage always yields a line for a governed order
    "needs_review": ActorPasses((JUDGES_ROUND, REVIEWS_GATE), none_because=(
        "no daemon pass of its own — the user reviews it, and "
        "`invariants.true_blockers` is what puts it in front of them")),
    "waiting_pr_merge": ActorPasses((
        # wo-d3a70c29: the poll still SELECTS a stalled row (`polls_pull_request` is untouched)
        _Pass("Daemon.poll_pull_requests", _pr_poll_why,
              "daemon.polls_and_can_act_on_pull_request",
              polls_and_can_act_on_pull_request),
        JUDGES_ROUND,
        REVIEWS_GATE,
    )),
    # Deliberately none: `project_store.NOT_RETRIED` excludes it and `Daemon.settle_work_order` returns...
    "budget_exhausted": ActorPasses((JUDGES_ROUND, REVIEWS_GATE), none_because=(
        "no daemon pass, by design — the actor is the user via "
        "`ops.set_work_order_budget`, because raising the budget resumes the order")),
}


def scheduled_actor(store: ProjectStore, wo: dict[str, Any],
                    now: float | None = None) -> Actor | None:
    """Which daemon pass will act on this work order next, or None"""
    for p in SCHEDULED_ACTORS[wo["status"]].passes:
        if p.test(store, wo, now=now):
            return Actor(pass_name=p.pass_name,
                         why=p.why(store, wo, now=now) if callable(p.why) else p.why,
                         predicate=p.predicate)
    return None


# -- morpheus: production validation of shipped PRs (docs/superpowers/specs/2026-10-21-morpheus-validates-releases-in-production.md)

def _morpheus_catalog() -> Catalog:
    """The registered catalog, or the defaults when none is (a fresh home still has a queue)"""
    from .catalog import OsConfig

    try:
        return load_catalog(_catalog_file())
    except (OpsError, CatalogError):
        return Catalog(os=OsConfig(), projects=[])


def _morpheus_repo(repo: str | None) -> Path:
    """Where the release's git history lives: given, else production's checkout, else here"""
    from .paths import production_code_dir

    if repo:
        return Path(repo).expanduser().resolve()
    prod = production_code_dir()
    return prod if (prod / ".git").exists() else Path.cwd()


def _morpheus_project(catalog: Catalog, project: str | None, slug: str = "") -> str:
    """The project a PR belongs to: named, else the one whose origin is `slug`, else the OS"""
    from . import morpheus, schedule

    if project:
        if catalog.projects and project not in {p.name for p in catalog.projects}:
            raise OpsError(f"project {project!r} is not in the catalog")
        return project
    for p in catalog.projects:
        if slug and p.path.is_dir() and morpheus.repo_slug(p.path) == slug:
            return p.name
    return schedule.os_project((p.name, p.path) for p in catalog.projects) or ""


def _morpheus_cwd(catalog: Catalog, item: dict[str, Any]) -> Path:
    """Where an item's script runs: its project's production directory"""
    from . import schedule
    from .paths import production_code_dir

    os_name = schedule.os_project((p.name, p.path) for p in catalog.projects)
    prod = production_code_dir()
    if item.get("project") == os_name and prod.is_dir():
        return prod
    for p in catalog.projects:
        if p.name == item.get("project") and p.path.is_dir():
            return p.path
    return prod if prod.is_dir() else Path.cwd()


def morpheus_report() -> dict[str, Any]:
    from . import morpheus

    central = CentralStore()
    try:
        return morpheus.report(central, _morpheus_catalog())
    finally:
        central.close()


def morpheus_item(item_id: str) -> dict[str, Any]:
    central = CentralStore()
    try:
        item = central.morpheus_item(item_id)
    finally:
        central.close()
    if not item:
        raise OpsError(f"no morpheus item {item_id!r} — `jarvis morpheus status` lists them")
    return item


def morpheus_enqueue_release(tag: str, from_ref: str | None = None,
                             to_ref: str | None = None,
                             repo: str | None = None,
                             project: str | None = None) -> dict[str, Any]:
    import subprocess

    from . import morpheus

    catalog = _morpheus_catalog()
    where = _morpheus_repo(repo)
    central = CentralStore()
    try:
        return morpheus.enqueue_release(
            central, catalog.os.morpheus, where, tag, from_ref=from_ref, to_ref=to_ref,
            project=_morpheus_project(catalog, project, morpheus.repo_slug(where)))
    except subprocess.CalledProcessError as e:
        raise OpsError(f"git could not read the release range: "
                       f"{(e.stderr or '').strip()[:300]}") from e
    finally:
        central.close()


def morpheus_add(pr: str, repo: str | None = None, release: str = "",
                 project: str | None = None) -> dict[str, Any]:
    """Queue one PR by number or URL, outside any release"""
    from . import morpheus

    m = re.search(r"(?:github\.com/([^/]+/[^/]+)/pull/)?#?(\d+)\s*$", pr.strip())
    if not m:
        raise OpsError(f"{pr!r} is not a PR number or URL")
    slug = m.group(1) or morpheus.repo_slug(_morpheus_repo(repo))
    if not slug:
        raise OpsError("cannot tell which repository — pass the PR's full URL")
    try:
        body = morpheus.fetch_pr(slug, int(m.group(2)), _morpheus_repo(repo))
    except Exception as e:  # noqa: BLE001
        raise OpsError(str(e)) from e
    catalog = _morpheus_catalog()
    central = CentralStore()
    try:
        item = morpheus.enqueue(central, catalog.os.morpheus, body,
                                project=_morpheus_project(catalog, project, slug),
                                repo=slug, release_tag=release)
    finally:
        central.close()
    if item is None:
        raise OpsError(f"PR #{body.number} is already queued — `jarvis morpheus status`")
    return item


def morpheus_set_script(item_id: str, script: str, source: str = "agent") -> dict[str, Any]:
    from . import morpheus

    central = CentralStore()
    try:
        return morpheus.set_script(central, item_id, script, source)
    except (KeyError, ValueError) as e:
        raise OpsError(str(e).strip("'\"")) from e
    finally:
        central.close()


def morpheus_check(item_id: str) -> dict[str, Any]:
    """Run an item's script now, the way the daemon would"""
    from . import morpheus

    item = morpheus_item(item_id)
    if not item["script"]:
        raise OpsError(f"{item_id} has no script yet — "
                       f"`jarvis morpheus script {item_id} --file <path>`")
    catalog = _morpheus_catalog()
    cwd = _morpheus_cwd(catalog, item)
    central = CentralStore()
    try:
        cfg = catalog.os.morpheus
        if item["state"] in morpheus.OPEN_STATES:
            morpheus.check(central, cfg, item, cwd)
            return central.morpheus_item(item_id) or {}
        # A settled item is only probed: its state is the record and stays put
        result = morpheus.run_script(item["script"], cwd, cfg.script_timeout_seconds)
        return {**item, "probe_exit": result.exit, "probe_output": result.output}
    finally:
        central.close()


def _close_validation_questions(item_id: str, answer: str, why: str) -> list[int]:
    """Supersede every open Neo question about this item: the user decided it on the item"""
    from . import morpheus
    from .neo_store import OPEN_Q_STATUSES, NeoStore

    neo = NeoStore()
    try:
        return [q["id"] for q in neo.list_questions(statuses=OPEN_Q_STATUSES)
                if q["kind"] == "validation" and morpheus.item_of_question(q) == item_id
                and neo.supersede(q["id"], answer, why)]
    finally:
        neo.close()


def morpheus_waive(item_id: str, reason: str) -> dict[str, Any]:
    from . import morpheus

    central = CentralStore()
    try:
        item = morpheus.waive(central, item_id, reason)
    except (KeyError, ValueError) as e:
        raise OpsError(str(e).strip("'\"")) from e
    finally:
        central.close()
    _close_validation_questions(item_id, f"The user waived {item_id}: {reason.strip()}",
                                f"decided by the user on {item_id} itself")
    return item


def morpheus_confirm(item_id: str, fix: str) -> dict[str, Any]:
    """The user rules a failed check real and files its fix: the user's own order, no steward"""
    from . import morpheus

    catalog = _morpheus_catalog()

    def file(item: dict[str, Any]) -> str:
        project = item.get("project") or morpheus._os_project(catalog)
        title = f"Fix PR #{item['pr_number']} failing production validation"
        return str(create_work_order(project, title,
                                     description=morpheus.fix_description(item, fix))["id"])

    central = CentralStore()
    try:
        item, order = morpheus.confirm(central, item_id, fix, file)
    except (KeyError, ValueError) as e:
        raise OpsError(str(e).strip("'\"")) from e
    finally:
        central.close()
    _close_validation_questions(item_id, f"The user confirmed {item_id} failed; fix: {order}",
                                f"decided by the user on {item_id} itself")
    return {**item, "fix_order": order}


def morpheus_pause(mode: str | None, reason: str = "") -> dict[str, Any]:
    """Pin Morpheus's session paused or running whatever the fleet does; None follows the fleet"""
    from . import morpheus

    central = CentralStore()
    try:
        return morpheus.set_pause(central, mode, reason)
    finally:
        central.close()


def morpheus_requeue(item_id: str) -> dict[str, Any]:
    from . import morpheus

    central = CentralStore()
    try:
        item = morpheus.requeue(central, _morpheus_catalog().os.morpheus, item_id)
    except KeyError as e:
        raise OpsError(str(e).strip("'\"")) from e
    finally:
        central.close()
    _close_validation_questions(item_id, f"The user put {item_id} back on the watch list",
                                f"decided by the user on {item_id} itself")
    return item
