"""High-level operations shared by the CLI, the web UI, and the Jarvis persona.

Every mutation of the OS goes through here, so all surfaces behave identically.
"""

from __future__ import annotations

import json
import os
import signal
import subprocess
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from .bootstrap import BootstrapReport, bootstrap_project, settings_drift
from .catalog import (
    Catalog,
    CatalogError,
    ProjectSpec,
    load_catalog,
    worker_stalls_on_prompts,
)
from . import db, invariants
from .sections import QUESTION_MAX_CHARS, QUESTION_WARN_CHARS
from .central_store import CentralStore
from .daemon import daemon_running
from .invariants import PR_CLOSED_BLOCKER, true_blockers
from .paths import daemon_pidfile, ensure_home, logs_dir
from .project_store import (
    FO_OPEN_STATUSES,
    FO_TERMINAL_STATUSES,
    OPEN_STATUSES,
    ProjectStore,
)


class OpsError(RuntimeError):
    """User-facing operational error."""


# -- catalog resolution ----------------------------------------------------------

def resolve_catalog(catalog_path: str | None = None) -> Catalog:
    """Load the catalog from an explicit path, or the one registered at start."""
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


def validation_enabled() -> bool:
    """Is `os.validation.enabled` on? False whenever the catalog cannot be read.

    The validation layer ships DISABLED, and at that default the OS must behave exactly
    as it does today. A catalog that has moved, been deleted or was never registered is
    therefore answered `False` rather than raised: the alternative is a release path that
    works today and fails tomorrow for a reason that has nothing to do with the plan being
    released. The catalog file is read on demand rather than cached because the daemon may
    be days old and the answer is only ever consulted at the moments a unit changes shape.
    """
    try:
        return bool(resolve_catalog().os.validation.enabled)
    except (OpsError, CatalogError, OSError, ValueError):
        return False


# -- OS lifecycle -------------------------------------------------------------------

def start_os(catalog_path: str, force_config: bool = False,
             foreground: bool = False, poll_interval: float = 5.0) -> dict[str, Any]:
    """Validate the catalog, bootstrap every project, register them, start jarvisd."""
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


# -- status ------------------------------------------------------------------------------

def run_doctor(project: str | None = None, repair: bool = False,
               catalog_path: str | None = None) -> dict[str, Any]:
    """Run the OS's post-condition checks over one project or the whole fleet.

    Read-only unless `repair` is set, so it is safe to run at any time. The daemon runs
    the same checks with repair enabled on every reconcile tick — this is the manual
    handle for "is the OS lying to me right now?".
    """
    from .invariants import check_catalog, check_os, check_project, check_release_marker

    # Catalog-level checks run whenever a catalog is resolvable at all: a gate that can
    # never open is a fault in the configuration, visible before any work order exists.
    config_violations: dict[str, list[Any]] = {}
    try:
        cat = resolve_catalog(catalog_path)
    except (OpsError, CatalogError):
        cat = None
    if cat is not None:
        for v in check_catalog(cat, project):
            config_violations.setdefault(v.context.get("project", ""), []).append(v)

    if catalog_path:
        # Explicit catalog: works before the OS has ever been started, when the
        # central registry is still empty.
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

    # OS-level checks first: they are about the OS itself (is the dashboard alive?),
    # not about any one project, and `--project` must not filter them out — a fleet
    # scoped to one project still wants to know its web UI is broken.
    os_found = check_os()
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
            found = check_project(store, repair=repair)
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
    # A catalog project the registry doesn't know about still gets its config reported —
    # a misconfigured gate matters most before the project has ever run.
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
    # OS-level state under $JARVIS_HOME, owned by no project: a pending-release marker
    # stuck in flight. Reported under its own heading; never repaired (which half of
    # the hand-off died is not derivable from the file).
    os_violations = check_release_marker()
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
    """Background agents named `[WO …]` that no open work order is driving.

    Every one of these is debris from the transport headless turns replaced: it forked a
    fresh background agent per delivered turn and retired the previous one on a
    best-effort `claude stop`, so each failed retirement leaked an agent permanently (the
    live fleet reached 63). Nothing creates them any more, and an in-flight work order
    releases its own on the next message it receives (`worker_session.send`), so what is
    left is the historical pile.

    Reported, never stopped: these live in the user's own agents view, and bulk-killing
    sessions there is theirs to authorise. Each row carries the exact command.
    """
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
    """How the dashboard is doing, from its own log on disk.

    Deliberately a plain read of `$JARVIS_HOME/logs/ui.log`: the UI runs in a separate
    process (its own systemd unit in production) so there is no live handle to ask, and
    the log is the only channel that survives it crashing outright.
    """
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
    """`(counts, questions Neo handed back to the user)` — one open of Neo's DB.

    `approval` and `plan` questions are dropped here rather than at the display: both are
    reported by the thing that actually carries the decision (the gate item, the feature
    order), and telling the user to `jarvis neo answer` a question whose real resolution
    is `jarvis gate approve` sends them to the wrong command.
    """
    from .neo_store import NeoStore

    neo = NeoStore()
    try:
        return (neo.counts(),
                [q for q in neo.list_questions(statuses=("escalated", "failed"))
                 if q.get("kind") not in ("approval", "plan")])
    finally:
        neo.close()


def os_status(catalog: Catalog | None = None) -> dict[str, Any]:
    central = CentralStore()
    try:
        pid = daemon_running()
        projects = []
        attention: list[dict[str, Any]] = []
        # Best-effort map of each project's worker permission mode, to catch a fleet
        # misconfigured into a mode that stalls background workers (see below).
        try:
            _cat = catalog or resolve_catalog()
            mode_by_project = {ps.name: ps.worker.permission_mode for ps in _cat.projects}
        except (OpsError, CatalogError):
            mode_by_project = {}
        # Read Neo's questions BEFORE the project loop, not after it: a question Neo sent
        # up already gets its own attention line below, carrying the text and the
        # `jarvis neo answer` command, and the work order it came from must not add a
        # second line saying the same thing less well. Same rule, and the same shape, as
        # `gate_held` inside the loop.
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
                # Attention isn't limited to open work orders: a FAILED worker
                # (e.g. session disappeared) still needs the user until acted on.
                flagged = {wo["id"]: wo for wo in open_wos if wo["needs_attention"]}
                for wo in store.list_work_orders():
                    if wo["needs_attention"]:
                        flagged.setdefault(wo["id"], wo)
                # Work orders held up by an escalated gate are reported once, below,
                # by the item that actually carries the command to run. Listing the
                # work order's own flag as well says the same thing twice and buries
                # the actionable line.
                gate_held = {a["wo_id"] for a in store.escalated_approvals()}
                # A feature order contributes ONE line to the strip, never one per child.
                # Its children keep their own flags — nothing is cleared, and they are
                # right there on the feature's page — but they are rolled up here rather
                # than listed. The comment on `waiting_pr_merge` in project_store.py
                # articulates the fear precisely: a strip that names everything is a strip
                # that stops being read, and a six-child feature is six lines for what the
                # user experiences as one piece of work.
                #
                # This is a change to how attention is PRESENTED. `true_blockers` stays
                # the single source of truth for whether a work order needs anyone, and
                # `jarvis wo list` still shows every flagged child individually.
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
                    # A worker blocked on a permission prompt can't be approved from
                    # jarvis — surface the native escape hatch instead. `--resume`, not
                    # `attach`: attaching is a background-agent verb and worker turns are
                    # headless, so the session is free to be opened directly between turns.
                    if wo["status"] == "waiting_input" and wo["session_id"]:
                        item["attach"] = f"claude --resume {wo['session_id']}"
                        # …and `jarvis wo resume-auto` ONLY where a prompt is possible at
                        # all. Offered unconditionally, it sent the user at a command that
                        # flips `auto` to `auto` and sends a message — no recovery, one
                        # conversation re-sent at the cache-write rate, and a worker that
                        # was waiting correctly interrupted (GitHub issue 100).
                        mode = mode_by_project.get(p["name"])
                        if mode and worker_stalls_on_prompts(mode):
                            item["resume_auto"] = f"jarvis wo resume-auto {wo['id']}"
                    attention.append(item)
                features = store.list_feature_orders(statuses=FO_OPEN_STATUSES)
                # Which feature orders get a line: the open ones, plus any that is asking
                # for the user or holds a flagged child. Both additions are about the same
                # status — `failed` is SETTLED, and it is also the one a feature order
                # raises its own flag in and the one that always leaves flagged children
                # behind. Scanning only the open list would drop the flag on the floor at
                # the moment it means the most, and would let those children back into the
                # strip individually just as the rollup was carrying the most lines.
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
                    kids = rolled_up.get(fo_id, [])
                    if not fo or not (fo["needs_attention"] or kids):
                        continue
                    reasons = []
                    if fo["needs_attention"] and fo["attention_reason"]:
                        reasons.append(fo["attention_reason"])
                    if kids:
                        reasons.append(
                            f"{len(kids)} of its work orders need you: "
                            + ", ".join(f"{k['id']} ({k['attention_reason']})"
                                        for k in kids)
                        )
                    progress = feature_progress(store, fo)
                    attention.append({
                        "project": p["name"], "wo_id": None, "fo_id": fo_id,
                        "title": fo["title"], "status": f"feature:{fo['status']}",
                        "reason": f"{progress['label']} — " + "; ".join(reasons),
                        "rolled_up": [k["id"] for k in kids],
                        "decide": f"jarvis fo show {fo_id}",
                    })
                drift = settings_drift(path / ".claude" / "settings.json")
                projects.append({
                    "name": p["name"], "path": p["path"],
                    "description": p["description"],
                    "summary": summary,
                    "feature_orders": [
                        {**{k: fo[k] for k in ("id", "title", "status",
                                               "needs_attention", "attention_reason")},
                         "progress": feature_progress(store, fo)}
                        for fo in features
                    ],
                    "open_work_orders": [
                        {**{k: wo[k] for k in ("id", "title", "status", "origin",
                                               "needs_attention", "attention_reason",
                                               "pr_url")},
                         # Why a pending work order is not starting. Derived here, with
                         # the store open, so every surface reading os_status gets the
                         # same answer as `jarvis wo list` instead of deriving its own.
                         "blocked_by": blocked_by(store, wo),
                         # Same rule, for the other reason a work order can be sitting
                         # still: the transport dropped its turn — the usage limit, or
                         # the API failing — and it retries itself at N.
                         "pause": invariants.pause_note(store, wo)}
                        for wo in open_wos
                    ],
                    "settings_drift": drift,
                })
                if drift:
                    attention.append({
                        "project": p["name"], "wo_id": None,
                        "title": "settings drift", "status": "config",
                        "reason": f".claude/settings.json: {drift}",
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
        for q in escalated_questions:
            attention.append({
                "project": q["project"], "wo_id": q["wo_id"],
                "title": f"Neo escalated: {q['question'][:80]}",
                "status": "neo_escalated",
                "reason": q.get("answer_reason") or "Neo declined to answer for you",
                "neo_question_id": q["id"],
                "decide": f"jarvis neo answer {q['id']} \"…\"",
            })
        # Gates Neo sent up. These are the only approval requests that cost the user
        # anything: the rest were decided without them, which is the point.
        gate_items = []
        # Gate requests that turned out not to be gated actions at all. Reported as a
        # number and never as an attention item: a classifier defect is the OS's problem,
        # not the user's, but the rate is the one signal that says whether the
        # recognisers are getting better.
        false_positives = 0
        for name, path in registered_project_paths().items():
            if not path.is_dir():
                continue
            store = ProjectStore(path)
            try:
                false_positives += store.dismissed_count()
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
        # The dashboard is part of the OS, so its failures belong in the OS's pulse.
        # Until this, a 500 on the work-order page was known only to the systemd
        # journal — `jarvis status` reported a healthy fleet while the UI was down.
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
        return {
            "daemon": {
                "running": pid is not None,
                "pid": pid,
                "catalog": central.get_state("catalog_path"),
            },
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
                      "false_positives": false_positives},
            "healthy": pid is not None and not attention,
        }
    finally:
        central.close()


# -- work orders -----------------------------------------------------------------------------

def registered_project_paths() -> dict[str, Path]:
    central = CentralStore()
    try:
        return {p["name"]: Path(p["path"]) for p in central.list_projects()
                if p["status"] == "active"}
    finally:
        central.close()


def create_work_order(project_name: str, title: str, description: str = "",
                      origin: str = "jarvis", model: str | None = None,
                      effort: str | None = None, permission_mode: str | None = None,
                      append_system_prompt: str | None = None,
                      backlog_id: str | None = None,
                      depends_on: list[str] | None = None,
                      parent_id: str | None = None) -> dict[str, Any]:
    """File a work order. `parent_id` files it UNDER a feature order.

    Until now the only way a work order acquired a parent was a plan release, because the
    only thing that filed one was a planner. A feature's project manager order files
    remediation work as the feature runs — that is its whole job — and remediation that
    landed outside the feature would not hold up its completion and would not appear in
    its child tree, which is to say it would not be part of the feature at all.

    An open feature order only: attaching a child to one that has already completed or
    failed would silently reopen a settled unit, and `Daemon.settle_features` would then
    have to decide what a new child means for a status the user has already been told.
    """
    paths = registered_project_paths()
    if project_name not in paths:
        raise OpsError(f"project {project_name!r} not registered "
                       f"(known: {sorted(paths)}). Run `jarvis start` first.")
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
        return store.create_work_order(
            title=title, description=description, origin=origin, model=model,
            effort=effort, permission_mode=permission_mode,
            append_system_prompt=append_system_prompt, backlog_id=backlog_id,
            depends_on=depends_on, parent_id=parent_id,
        )
    except (KeyError, ValueError) as e:
        # A dependency on a work order in another project cannot be honoured — the edge
        # is resolved inside one project database — so say which project was searched
        # rather than letting a bare KeyError reach the terminal as a traceback.
        raise OpsError(f"cannot create the work order: {e} "
                       f"(dependencies are resolved within {project_name!r})") from e
    finally:
        store.close()


def blocked_by(store: ProjectStore, wo: dict[str, Any]) -> list[dict[str, Any]]:
    """Unfinished dependencies, without a query for the overwhelming majority.

    `depends_on` is already on the row, so a work order with no edges — nearly all of
    them — is answered from memory rather than costing a lookup per listing entry.
    """
    if not store.dependencies(wo):
        return []
    return store.unfinished_dependencies(wo["id"])


def find_work_order(wo_id: str, project_name: str | None = None
                    ) -> tuple[str, Path, dict[str, Any]]:
    """Locate a work order across all registered projects."""
    paths = registered_project_paths()
    # Guard before the lookup, not after: callers (the CLI, the dashboard) only catch
    # OpsError, so an unregistered name reaching `paths[...]` surfaces as a bare
    # KeyError — a traceback in the terminal and an HTTP 500 in the browser.
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
    """Which registered project owns this directory, if any.

    Longest match wins, so a project nested inside another resolves to the inner one
    rather than to whichever happened to be checked first.
    """
    best: tuple[int, str] | None = None
    for name, path in paths.items():
        root = str(path)
        if cwd == root or cwd.startswith(root.rstrip("/") + "/"):
            if best is None or len(root) > best[0]:
                best = (len(root), name)
    return best[1] if best else None


def injectable_sessions(project_name: str, timeout: int = 5) -> dict[str, Any]:
    """The user's live Claude sessions under a project that Jarvis is not tracking.

    Read-only, and deliberately fail-soft: this feeds a dashboard panel, and shelling out
    to `claude agents --json` on a web request must never be the reason a page breaks. On
    any failure it returns an `error` for the template to show inline instead of raising.
    """
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
    """Hand a Claude session the user started over to Jarvis, as a work order.

    This is the ONLY way a session the user opened enters the OS. Jarvis does not adopt
    sessions it finds any more (GitHub issue 47): one running under a registered project
    path is the user's private conversation until they say otherwise.

    Injection creates the record and nothing else. It does not rename the session, does
    not send it a turn, and writes nothing into it — the first write is the user's own
    `jarvis wo send` / `jarvis wo resume-auto`, which is a separate, explicit act. From
    here the daemon tracks the session's state (running / blocked / ended) and, exactly
    as before, never holds it to the worker contract.
    """
    from . import claude_cli

    if not claude_cli.available():
        raise OpsError("the `claude` CLI is not on PATH, so its sessions cannot be read")
    try:
        roster = claude_cli.list_background_sessions()
    except claude_cli.ClaudeCliError as e:
        raise OpsError(f"could not list Claude sessions: {e}") from e

    # Accept either identifier: `claude agents --json` reports both a session id and its
    # own agent id, and the two namespaces do not overlap.
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
        # Re-injecting is a no-op rather than an error: the point is that Jarvis knows
        # about the session, and it already does. A duplicate row would split its history.
        existing = store.find_by_session(match.session_id)
        if existing:
            note = f"already tracked as {existing['id']} ({existing['origin']})"
            # Re-injecting a session that was retired when it went idle picks tracking
            # back up. The daemon cannot do this on its own any more: it stops reading
            # the roster once a project has no live injected session, which is what keeps
            # `claude agents --json` off the tick for everyone who never injects.
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
        # Mirror the session's current state, exactly as the daemon's tracker would. The
        # status goes in at INSERT time: a row that is `pending` for even one daemon tick
        # would be claimed and dispatched, which is a worker turn in the user's session.
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


def send_message(wo_id: str, content: str, source: str = "jarvis",
                 project_name: str | None = None) -> dict[str, Any]:
    name, path, wo = find_work_order(wo_id, project_name)
    if wo["status"] in ("completed", "failed", "cancelled"):
        # Still allowed — resuming a finished session is fine — but tell the user.
        note = f"note: work order is {wo['status']}; the session will be revived"
    else:
        note = None
    store = ProjectStore(path)
    try:
        msg_id = store.queue_message(wo_id, content, source=source)
        store.add_event(wo_id, "message_queued", {"msg_id": msg_id, "source": source})
        # A reply IS the response to whatever flagged the user — drop it from the
        # attention list now, don't wait for the daemon to deliver. The message
        # stays queued for the worker; if delivery later fails the daemon re-flags.
        if wo["needs_attention"]:
            store.clear_attention(wo_id)
    finally:
        store.close()
    return {"project": name, "wo_id": wo_id, "msg_id": msg_id, "note": note,
            "delivery": "jarvisd delivers when the worker is idle"}


def waiting_on(store: ProjectStore, wo: dict[str, Any]) -> dict[str, Any]:
    """What this work order is actually waiting for, and whether a nudge could help.

    `{"what": <slug>, "detail": <sentence naming the way through>, "stalled": bool}`.
    `stalled` is the narrow claim "nothing is coming for this by itself" — the only
    condition under which sending it a message is a repair rather than an interruption.

    Ordered like `invariants.true_blockers`, most-actionable first, and it agrees with it
    by construction on everything both can see. It goes further deliberately: that
    function answers "does this need the USER", and this one answers "what is this
    waiting for", which for most of these is Neo, the daemon, or nothing at all.
    """
    from .invariants import awaiting_neo, neo_question_blocker
    from .neo_store import USER_HELD_Q_STATUSES

    wo_id = wo["id"]
    pending = store.pending_assumptions(wo_id)
    if pending:
        return {"what": "assumptions", "stalled": False,
                "detail": f"{len(pending)} assumption(s) await your review — "
                          f"`jarvis wo review {wo_id}`"}
    escalated = store.escalated_approvals(wo_id)
    if escalated:
        return {"what": "gate_escalated", "stalled": False,
                "detail": f"a gate Neo sent up to you — `jarvis gate approve "
                          f"{escalated[0]['id']} --reason \"…\"` (or `deny`)"}
    if store.pending_approvals(wo_id):
        return {"what": "gate_with_neo", "stalled": False,
                "detail": "a privileged-action gate that is with Neo — the verdict "
                          "reaches the worker by itself"}
    question = awaiting_neo(wo_id)
    if question is not None:
        if question["status"] in USER_HELD_Q_STATUSES:
            return {"what": "neo_escalated", "stalled": False,
                    "detail": neo_question_blocker(question)}
        return {"what": "neo_question", "stalled": False,
                "detail": f"Neo is answering question {question['id']} — the answer "
                          f"arrives as the worker's next turn by itself"}
    queued = store.queued_messages(wo_id)
    if queued:
        return {"what": "queued_message", "stalled": False,
                "detail": f"{len(queued)} message(s) queued — jarvisd delivers them "
                          f"when the worker is idle"}
    # A live turn means "working" for every status EXCEPT `waiting_input`, where it means
    # the opposite: a permission prompt blocks INSIDE the turn, so the process is alive
    # and going nowhere. That is the one case this command was written for, and reading
    # the turn row the other way round would make it refuse the only thing it can fix.
    turn = store.latest_turn(wo_id)
    if (turn is not None and turn["state"] == "running"
            and wo["status"] != "waiting_input"):
        return {"what": "turn_running", "stalled": False,
                "detail": "a turn is in flight — the worker is working"}
    if wo["status"] in ("completed", "cancelled", "failed", "waiting_pr_merge",
                        "needs_review"):
        return {"what": wo["status"], "stalled": False,
                "detail": f"the work order is {wo['status']} — nothing is running to "
                          f"nudge"}
    if wo["status"] == "pending":
        return {"what": "pending", "stalled": False,
                "detail": "not dispatched yet — no worker exists to nudge"}
    return {"what": "prompt", "stalled": True,
            "detail": "nothing else accounts for it: an unanswered permission prompt "
                      "is what is left"}


def resume_in_auto(wo_id: str, project_name: str | None = None,
                   force: bool = False) -> dict[str, Any]:
    """Diagnose what a work order is waiting on, and unstick it only if a nudge can.

    THE PREMISE OF THIS COMMAND IS USUALLY FALSE, which is why it diagnoses first. It
    was written to recover a worker stalled on a permission prompt, by flipping it to
    `auto` and nudging it. But `auto` is the fleet-wide default
    (`catalog.DEFAULT_PERMISSION_MODE`) and no project overrides it, so the flip is
    `auto → auto` — and a worker in a mode that never prompts has never stalled on one.
    All the command actually did was send a message, and a message is not free: every
    turn boundary re-sends the whole conversation at the cache-write rate (~12% of fleet
    spend), and the nudge lands on a worker that was very often mid-wait and correct. On
    wo-52a6164d it was run against a worker that had never stalled, was mid-turn, and
    finished the release unaided half an hour later (GitHub issue 100).

    So: report what the work order is really waiting on, and refuse to nudge when the
    mode already cannot prompt and something else — Neo, the daemon, a turn in flight —
    is what it waits for. `force=True` sends the nudge anyway, for the user who has
    diagnosed it themselves and wants the worker poked.
    """
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
    if not force and not could_prompt and not wait["stalled"]:
        # The no-op case, reported rather than performed. Recorded on the timeline too:
        # "the user asked what was wrong and the OS said nothing was" is part of this
        # work order's history, and it is the evidence that the nudge did not happen.
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
    """The mode this project's workers run in, from the catalog — None if unreadable.

    A work order's own `permission_mode` is usually NULL and resolved against this at
    send time (`worker_session.turn_args`), so the column alone cannot answer "can this
    worker be prompted at all".
    """
    try:
        catalog = resolve_catalog()
    except (OpsError, CatalogError):
        return None
    for spec in catalog.projects:
        if spec.name == project_name:
            return spec.worker.permission_mode
    return None


def assume(wo_id: str, content: str) -> dict[str, Any]:
    """Record an assumption: DB row + ASSUMPTIONS.md append + review flag."""
    name, path, wo = find_work_order(wo_id)
    store = ProjectStore(path)
    try:
        store.add_assumption(wo_id, content)
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


def validation_config() -> Any:
    """The OS's validation settings, or None when they cannot be read.

    Best-effort on purpose, and it is `os_status`'s pattern rather than a new one: a
    worker calling `jarvis wo finish` from a checkout whose catalog has moved, or with
    no catalog registered at all, must still be able to finish. No catalog means no
    validation, which is the shipped default anyway.
    """
    try:
        return resolve_catalog().os.validation
    except (OpsError, CatalogError, OSError):
        return None


def land_finished(store: ProjectStore, wo: dict[str, Any],
                  pr_url: str | None = None) -> str:
    """Where a work order that has genuinely finished lands, and the backlog item it
    closes on the way. Shared by `finish` and by the round machine's PASS.

    One function because there are now two routes to the same ending, and they must not
    drift: the day validation is enabled, a work order that passes has to land exactly
    where the same work order lands with the feature switched off — `waiting_pr_merge`
    with a pull request, `completed` without one, and the backlog item closed in the
    `completed` case only.
    """
    wo_id = wo["id"]
    pr_url = pr_url or wo.get("pr_url") or None
    status = "waiting_pr_merge" if pr_url else "completed"
    store.set_status(wo_id, status)
    store.clear_attention(wo_id)
    if wo.get("backlog_id") and status == "completed":
        central = CentralStore()
        try:
            central.mark_backlog(wo["backlog_id"], "done")
        finally:
            central.close()
    return status


def submit_for_validation(store: ProjectStore, project_path: Path, wo: dict[str, Any],
                          *, declared: str, cfg: Any) -> dict[str, Any]:
    """Open a validation round over what this work order has produced.

    Collects the evidence, fingerprints it, opens the round and parks the work order in
    `validating`. It judges nothing: the daemon runs the validator off its tick thread
    and settles what comes back.

    **The round number is COUNTED, not derived from the row count** — a submission that
    is retried while its round is still open, or one that follows a transport outage,
    reuses the number it already has. The insert is idempotent per (work order, round),
    so two callers racing here produce one round rather than two.
    """
    from . import evidence as evidence_mod

    packet = evidence_mod.collect_work_order(
        project_path, wo, declared=declared, diff_chars=cfg.diff_chars)
    nxt = store.counted_validation_rounds(wo_id=wo["id"]) + 1
    round_row = store.open_validation_round(
        wo_id=wo["id"], fingerprint=evidence_mod.fingerprint(packet),
        summary=str(wo.get("result_summary") or ""), evidence=declared,
        pr_url=wo.get("pr_url"), round=nxt)
    store.set_status(wo["id"], "validating")
    # No attention flag: a unit under review is the system working. Only the give-up
    # transition flags anyone.
    store.clear_attention(wo["id"])
    store.add_event(wo["id"], "validation_submitted",
                    {"round": round_row["round"], "round_id": round_row["id"],
                     "fingerprint": round_row["fingerprint"],
                     "files": len(packet.files)})
    return round_row


def finish(wo_id: str, summary: str, pr_url: str | None = None,
           evidence: str = "") -> dict[str, Any]:
    """The worker reporting its own result.

    `pr_url` is what separates "delivered" from "delivered and merged": a work order
    that ends in a pull request is not finished until a human merges it, so it settles
    into `waiting_pr_merge` and stays on the open list with the link, instead of going
    to `completed` and disappearing into the settled group nobody reads. The merge is
    what ends it: `Daemon.poll_pull_requests` watches the PR and completes the work
    order itself, and `jarvis wo done` remains the manual exit for a PR that will never
    merge.

    Pending assumptions still outrank it: those are a decision the OS is waiting on,
    and a PR the user merges before deciding them accepts them by the back door. That
    makes `review_work_order` the only route back for such a work order, and it is that
    function's job to do the parking skipped here.

    `evidence` is the worker's own account of how it tested the change, and it is
    OPTIONAL: every worker in flight when this shipped predates the flag, so an empty
    one is an ordinary submission and not a thin one.

    **This is the one place `os.validation.enabled` is read**, and it gates OPENING a
    round and nothing else. A flag turned off while rounds are open must still let the
    daemon judge and settle them — otherwise the only control the user has over a
    misbehaving panel would strand every unit already inside it.
    """
    name, path, _wo = find_work_order(wo_id)
    cfg = validation_config()
    store = ProjectStore(path)
    try:
        fields: dict[str, Any] = {"result_summary": summary}
        if pr_url:
            fields["pr_url"] = pr_url
        store.update_work_order(wo_id, **fields)
        fresh = store.get_work_order(wo_id)
        if store.pending_assumptions(wo_id):
            store.set_status(wo_id, "needs_review")
            store.flag_attention(wo_id, "assumptions pending review")
            status = "needs_review"
        elif cfg is not None and cfg.enabled:
            submit_for_validation(store, path, fresh, declared=evidence, cfg=cfg)
            status = "validating"
        else:
            status = land_finished(store, fresh, pr_url)
        store.add_event(wo_id, "finished", {"summary": summary,
                                            **({"pr_url": pr_url} if pr_url else {})})
    finally:
        store.close()
    return {"project": name, "wo_id": wo_id, "status": status,
            **({"pr_url": pr_url} if pr_url else {})}


def mark_done(wo_id: str, project_name: str | None = None) -> dict[str, Any]:
    """The user closing a work order themselves: "this is finished, stop tracking it".

    Distinct from all three neighbours, which is why it exists. `finish` is the *worker*
    reporting its own result and carries a summary the user does not have. `cancel` says
    the work should not happen, which is the wrong thing to record about work that did.
    `hide` only stops showing the record, leaving it open forever.

    Two behaviours it borrows deliberately:

    - It stops the worker, exactly as `cancel` does. A work order nobody is reading any
      more must not leave a process burning tokens and editing its worktree.
    - It refuses while assumptions are pending, exactly as `ack_attention` does. Those
      are decisions the OS is waiting on, and closing over them would silently accept
      them on the user's behalf. `jarvis wo review` is the way through.

    An existing `result_summary` is left alone: whatever the worker last reported is
    still the truest thing on the record, and this is not a claim about the outcome.
    """
    name, path, wo = find_work_order(wo_id, project_name)
    store = ProjectStore(path)
    try:
        if store.pending_assumptions(wo_id):
            raise OpsError(
                f"{wo_id} is waiting on a decision (assumptions pending review) — "
                f"marking it done would accept them silently. Use `jarvis wo review "
                f"{wo_id}` to accept, or `--reject` to send it back."
            )
        stopped = close_out(store, wo, "marked_done", why="work order marked done")
    finally:
        store.close()
    mark_backlog_done(wo)
    return {"project": name, "wo_id": wo_id, "title": wo["title"],
            "status": "completed", "was": wo["status"],
            "session_stopped": stopped["stopped"]}


def close_out(store: ProjectStore, wo: dict[str, Any], event: str, *, why: str,
              payload: dict[str, Any] | None = None) -> dict[str, Any]:
    """Settle a work order as `completed` and take its worker down with it.

    The mechanics shared by every "this is over, and it went fine" path: `jarvis wo
    done` (the user saying so) and a merged pull request (GitHub saying so). Both end
    the same way and must keep ending the same way — the only difference between them
    is `event`, which is what the timeline shows for who decided.

    Does NOT check pending assumptions: the two callers disagree about that. `mark_done`
    refuses over them, because closing by hand would accept them silently; the merge
    poller never sees one, because `finish` routes a work order with assumptions to
    `needs_review` instead of parking it behind its PR.

    Callers own the backlog side (`mark_backlog_done`), since they hold the row.
    """
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


def mark_backlog_done(wo: dict[str, Any]) -> None:
    """Close the backlog item a work order was promoted from, if it came from one."""
    if not wo.get("backlog_id"):
        return
    central = CentralStore()
    try:
        central.mark_backlog(wo["backlog_id"], "done")
    finally:
        central.close()


def complete_merged(store: ProjectStore, wo: dict[str, Any],
                    merged_at: str | None = None) -> dict[str, Any]:
    """The pull request landed: end the work order, exactly as the user closing it does.

    This is the whole point of polling GitHub. `jarvis wo finish --pr` parks a work
    order in `waiting_pr_merge` precisely because the merge is the real ending, and
    until now the OS could not see that ending happen — so every finished work order
    sat on the open list until the user hand-typed `jarvis wo done`. On a fleet where
    one work order can depend on another having landed, that hand-typing is the
    schedule, which is why this had to exist before work orders could depend on
    each other at all.

    Records `pr_merged` rather than `marked_done`: the record must not claim the user
    did something they did not do.
    """
    store.update_work_order(wo["id"], pr_state="MERGED")
    stopped = close_out(store, wo, "pr_merged", why="pull request merged",
                        payload={"pr_url": wo.get("pr_url"), "merged_at": merged_at})
    mark_backlog_done(wo)
    return {"wo_id": wo["id"], "status": "completed", "was": wo["status"],
            "pr_url": wo.get("pr_url"), "merged_at": merged_at,
            "session_stopped": stopped["stopped"]}


def record_pr_closed(store: ProjectStore, wo: dict[str, Any]) -> dict[str, Any]:
    """The pull request was closed without merging: the delivered work was refused.

    The opposite of `complete_merged` and not a variant of it. Nothing landed, so the
    work order cannot be completed; and leaving it in `waiting_pr_merge` would keep it
    in a merge queue waiting for a merge that is never coming. It goes to `needs_review`
    and asks for the user, which is what the attention list is for — someone shut this
    pull request on purpose and only they know whether the work should be redone,
    redirected or dropped.

    The worker is left alone: it finished long ago, and there is nothing here for it to
    do without a human deciding what "refused" means. `jarvis wo send` restarts it if
    the answer is "try again", `jarvis wo done` closes it if the answer is "drop it".
    """
    store.update_work_order(wo["id"], pr_state="CLOSED")
    store.add_event(wo["id"], "pr_closed", {"pr_url": wo.get("pr_url"),
                                            "was": wo["status"]})
    store.set_status(wo["id"], "needs_review")
    store.flag_attention(wo["id"], PR_CLOSED_BLOCKER)
    return {"wo_id": wo["id"], "status": "needs_review", "was": wo["status"],
            "pr_url": wo.get("pr_url")}


def _awaiting_merge(wo: dict[str, Any]) -> bool:
    """True when this work order's ending is still a pull request nobody has merged.

    The condition for putting a work order into `waiting_pr_merge` from anywhere other
    than `finish`. `pr_state` is what the merge poll last saw, and both of its values
    rule the merge queue out: MERGED already ended the work order, and CLOSED means the
    pull request is never merging — parking on a closed PR would put the work order back
    in front of a poll whose only possible move is to flag it for the user again.
    """
    return bool(wo.get("pr_url")) and wo.get("pr_state") not in ("MERGED", "CLOSED")


def stop_worker_session(wo: dict[str, Any], store: ProjectStore) -> dict[str, Any]:
    """Take the worker down with the work order, if anything of it is still running.

    Cancelling or deleting a work order has to stop its worker: nobody reads the output
    any more, but the process keeps going — burning tokens and editing its worktree.

    Two things can be running, and both are checked. A headless turn is a process Jarvis
    owns, killed by process group. A background agent is only possible for a work order
    created under the old transport, and is released with `claude stop`.

    Best effort by design: the caller's state change must never depend on a process
    being killable or the CLI being reachable, so every failure comes back as
    `stopped: False` with a reason instead of raising.
    """
    from . import claude_cli, worker_session

    killed = worker_session.cancel(store, wo["id"])
    if killed["stopped"]:
        return {"stopped": True, "pid": killed["pid"],
                "session_id": wo.get("session_id")}

    # Nothing of ours in flight. A legacy work order may still have a background agent.
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
    """Acknowledge attention flags — "I have seen this, stop showing it to me".

    The missing counterpart to `jarvis inbox ack`. Attention does not live in the inbox:
    it is a flag on each work order, re-derived from state on every reconcile tick. So
    acking the whole inbox left the attention list untouched, and clearing a flag by
    hand lasted until the next tick put it straight back. This is the only way to put
    one down for good.

    Pending assumptions are never acknowledgeable: they are a decision the OS is waiting
    on, and burying one silently drops work the user asked for. `jarvis wo review`
    (accept) or `--reject` is the way through those.
    """
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
                needs_decision = [b for b in blockers if "assumption" in b.lower()]
                if needs_decision:
                    if wo_id:
                        raise OpsError(
                            f"{wo['id']} is waiting on a decision ({needs_decision[0]}) "
                            f"— acknowledging would bury it. Use `jarvis wo review "
                            f"{wo['id']}` to accept, or `--reject` to send it back."
                        )
                    skipped.append({"wo_id": wo["id"], "reason": needs_decision[0]})
                    continue
                store.ack_attention(wo["id"], blockers)
                acknowledged.append(wo["id"])
        finally:
            store.close()
    return {"acknowledged": acknowledged, "skipped": skipped}


def hide_work_order(wo_id: str, hidden: bool = True,
                    project_name: str | None = None) -> dict[str, Any]:
    """Hide a work order from listings, summaries and the attention list.

    Nothing is destroyed and a running session is left alone — this is the user
    saying "stop showing me this", not "stop this".
    """
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
    """Cut the dependency edges holding a pending work order back.

    By default only the edges that can never clear — a dependency cancelled, failed or
    deleted — because those are the ones that strand it; a dependency still working is
    doing exactly what the edge was drawn for and releasing the dependent early would
    hand it a worktree without the code it was told to build on. `drop_all` is the
    override for a user who wants it to run anyway, and says so.
    """
    from . import invariants

    name, path, wo = find_work_order(wo_id, project_name)
    store = ProjectStore(path)
    try:
        blockers = store.unfinished_dependencies(wo_id)
        if not blockers:
            raise OpsError(f"{wo_id} is not blocked by anything")
        cut = blockers if drop_all else invariants.dead_dependencies(store, wo)
        if not cut:
            raise OpsError(
                f"{wo_id} is waiting on work that is still live "
                f"({', '.join(d['id'] for d in blockers)}), not stranded. "
                f"Pass --all to cut those edges anyway."
            )
        remaining = store.drop_dependencies(wo_id, [d["id"] for d in cut])
        # The stranding was the blocker; with the edge gone the work order is ordinary
        # pending again, and leaving the flag up would keep asking about a settled thing.
        if not remaining:
            store.clear_attention(wo_id)
    finally:
        store.close()
    return {"project": name, "wo_id": wo_id, "title": wo["title"],
            "dropped": [d["id"] for d in cut], "still_blocked_by": remaining}


def delete_work_order(wo_id: str, project_name: str | None = None) -> dict[str, Any]:
    """Erase a work order everywhere: project DB, central inbox/backlog, Neo's questions.

    Irreversible. The worker's session goes with it — once the record is gone there is
    nothing left to reattach a running agent to.
    """
    name, path, wo = find_work_order(wo_id, project_name)
    store = ProjectStore(path)
    try:
        stopped = stop_worker_session(wo, store)
        deleted = store.delete_work_order(wo_id)
    finally:
        store.close()
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


def review_work_order(wo_id: str, accept: bool = True,
                      feedback: str = "") -> dict[str, Any]:
    """Accept (or reject) all pending assumptions and settle the work order.

    `feedback` is where the user's reasoning goes, and it does two jobs that used to
    need two more commands: it becomes a Neo learning (so the decisions the user makes
    today train the agent meant to make them tomorrow), and on a rejection it is
    delivered to the still-open worker as guidance.

    Accepting settles the work order the way `finish` would have if the assumptions had
    never existed — which for a work order behind an unmerged pull request is
    `waiting_pr_merge`, NOT `completed`. `finish` deliberately routes a work order with
    pending assumptions to `needs_review` even when it carries a PR (the decision
    outranks the merge), so this review is the only route back and it owes that work
    order the parking `finish` skipped. Completing it here loses the PR twice: off the
    user's open list, and out of `Daemon.poll_pull_requests`, which only ever looks at
    `waiting_pr_merge` — so the merge that should have ended the work order unattended
    ends nothing.
    """
    name, path, wo = find_work_order(wo_id)
    store = ProjectStore(path)
    try:
        pending = store.pending_assumptions(wo_id)
        for a in pending:
            store.review_assumption(a["id"], "accepted" if accept else "rejected")
        status = wo["status"]
        if wo["status"] == "needs_review":
            if accept:
                status = "waiting_pr_merge" if _awaiting_merge(wo) else "completed"
                store.set_status(wo_id, status)
                store.clear_attention(wo_id)
            elif not feedback:
                # With feedback the guidance is delivered below, so the work order is
                # not waiting on the user — only a bare rejection strands it.
                store.flag_attention(wo_id, "assumptions rejected — send guidance with `jarvis wo send`")
        store.add_event(wo_id, "reviewed", {"accepted": accept, "count": len(pending),
                                            "feedback": feedback})
    finally:
        store.close()

    out = {"project": name, "wo_id": wo_id, "reviewed": len(pending), "accepted": accept,
           "status": status}
    if not feedback:
        return out

    from . import neo as neo_mod
    from .neo_store import NeoStore
    neo = NeoStore()
    try:
        learning = neo.add_learning(
            neo_mod.learning_from_assumption_review(wo, pending, accept, feedback),
            project=name, source="review",
        )
    finally:
        neo.close()
    out["learning_id"] = learning["id"]

    # A rejection without guidance reaching the worker just strands it. Deliver it.
    if not accept and wo["status"] in OPEN_STATUSES:
        try:
            out["delivered"] = send_message(wo_id, feedback, source="jarvis",
                                            project_name=name)
        except OpsError as e:
            out["delivery_error"] = str(e)
    return out


# -- feature orders --------------------------------------------------------------------------

def create_feature_order(project_name: str, title: str, description: str = "",
                         origin: str = "jarvis",
                         backlog_id: str | None = None,
                         max_parallel: int | None = None) -> dict[str, Any]:
    """File the coarse ask. Nothing is decomposed here — the daemon opens a planner.

    Deliberately the same shape as `create_work_order`, because the whole point of the
    `jarvis fo` surface is that a user who knows `jarvis wo` already knows it. What the
    user types is identical; what the OS does with it is not.

    `max_parallel` caps how many of this feature's children run at once. It is the USER's
    knob, not the planner's (ruled 2026-08-03): the design calls slot budgeting the
    planner's job, but a planner that budgets its own slots can hand itself the whole
    project's concurrency, and it would become one more thing the plan validator has to
    police. NULL — the default — means the project-wide `max_concurrent` is the only cap,
    which is exactly the behaviour every feature order had before this existed.
    """
    paths = registered_project_paths()
    if project_name not in paths:
        raise OpsError(f"project {project_name!r} not registered "
                       f"(known: {sorted(paths)}). Run `jarvis start` first.")
    if max_parallel is not None and max_parallel < 1:
        raise OpsError("--max-parallel must be at least 1 (omit it for no cap)")
    if not (description or "").strip():
        # A work order can survive a bare title — a human reads it and fills the gaps.
        # A feature order cannot: its first reader is a planner in a fresh session with
        # no memory of the conversation that produced it, and a planner given four words
        # will decompose four words.
        raise OpsError(
            f"a feature order needs a description: the planner sees only this text, "
            f"and it is what the whole decomposition is built from. Use "
            f"`jarvis fo create {project_name} \"{title[:40]}\" -d \"...\"`."
        )
    store = ProjectStore(paths[project_name])
    try:
        return store.create_feature_order(title=title, description=description,
                                          origin=origin, backlog_id=backlog_id,
                                          max_parallel=max_parallel)
    finally:
        store.close()


def find_feature_order(fo_id: str, project_name: str | None = None
                       ) -> tuple[str, Path, dict[str, Any]]:
    """Locate a feature order across all registered projects. Mirrors
    `find_work_order`, including its guard: callers only catch `OpsError`, so an
    unregistered name must not surface as a bare `KeyError`."""
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


def feature_progress(store: ProjectStore, fo: dict[str, Any]) -> dict[str, Any]:
    """How far along this feature order is, derived from its children every time.

    Never stored. The feature order's status says which PHASE it is in; the counts say
    where inside the phase it is, and they are a fact about the child rows — the same
    reasoning that keeps "blocked" out of `WO_STATUSES`. A stored 3/6 is a 3/6 that goes
    wrong the first time somebody cancels a child by hand.
    """
    children = store.feature_children(fo["id"])
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
                        include_settled: bool = False) -> list[dict[str, Any]]:
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
            for fo in store.list_feature_orders(statuses=statuses):
                out.append({"project": name, **fo,
                            "progress": feature_progress(store, fo)})
        finally:
            store.close()
    return out


def show_feature_order(fo_id: str, project_name: str | None = None) -> dict[str, Any]:
    """The feature order, its plan and its children — the tree, in one call."""
    from . import plans

    name, path, fo = find_feature_order(fo_id, project_name)
    store = ProjectStore(path)
    try:
        plan = db.from_json(fo.get("plan"), None)
        children = [
            {**{k: c[k] for k in ("id", "title", "status", "needs_attention",
                                  "attention_reason", "pr_url")},
             "depends_on": store.dependencies(c),
             "status_label": invariants.status_label(store, c)}
            for c in store.feature_children(fo_id)
        ]
        planner = None
        if fo.get("plan_wo_id"):
            try:
                p = store.get_work_order(fo["plan_wo_id"])
                planner = {k: p[k] for k in ("id", "title", "status", "result_summary")}
            except KeyError:
                planner = None  # deleted out from under it; the link was released
        return {
            "project": name, **fo,
            "plan": plan,
            "plan_text": "\n".join(plans.render_plan(plan)) if plan else "",
            "planner": planner,
            "children": children,
            "progress": feature_progress(store, fo),
            # Only meaningful next to `max_parallel`, but returned unconditionally so a
            # caller never has to branch on whether the key is there.
            "active_children": store.count_active_children(fo_id),
        }
    finally:
        store.close()


def submit_plan(fo_id: str, doc: Any,
                project_name: str | None = None) -> dict[str, Any]:
    """(Planners) hand back the decomposition. The planner's terminal action.

    Three things happen here and the order matters. The plan is validated first, so a
    bad plan costs a revision and nothing else — no work order, no Neo call, no state to
    unwind. Then it is stored and queued for review. Only then is the planner's work
    order settled: `jarvis fo plan` IS its `jarvis wo finish`, which is why the planner
    briefing tells it not to call the latter. A rejection later re-opens the same session
    through the ordinary message path, so settling now costs the revision nothing.
    """
    from . import plans
    from .neo_store import NeoStore

    name, path, fo = find_feature_order(fo_id, project_name)
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

    # The design document is snapshotted NOW, from the planner's own tree, because the
    # children never see that tree: the doc rides in the stored plan and dispatch
    # materialises it into a path every child worker can read. Refusing a dangling name
    # here costs the planner one revision; accepting it would cost every child a brief
    # that references a document none of them has.
    if plan.get("design_doc"):
        candidates = []
        if fo.get("plan_wo_id"):
            candidates.append(path / ".claude" / "worktrees" / fo["plan_wo_id"]
                              / plan["design_doc"])
        candidates.append(path / plan["design_doc"])
        existing = next((c for c in candidates if c.is_file()), None)
        if existing is None:
            raise OpsError(
                f"the plan names design_doc {plan['design_doc']!r} but no such file "
                f"exists — write it before submitting (looked in: "
                + ", ".join(str(c) for c in candidates) + ")"
            )
        plan["design_doc_content"] = existing.read_text()

    # The planner is who Neo's question hangs off: it is a real work order, it is who
    # receives a rejection, and it is what `jarvis neo list` can link back to. A feature
    # order whose planner was deleted still submits — the question just names the feature
    # order instead of a row that no longer exists.
    planner_id = fo.get("plan_wo_id") or fo_id
    question = plans.build_plan_question(fo, plan)
    neo = NeoStore()
    try:
        q = neo.ask(name, planner_id, question, kind="plan")
    finally:
        neo.close()

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
        # The planner has no more to say until the review lands, and a work order left
        # `running` with no turn in flight is what the reconciler calls idle.
        out["planner"] = finish(
            fo["plan_wo_id"],
            f"submitted a plan for {fo_id}: {len(plan['children'])} work orders",
        )
    return out


def review_plan(fo_id: str, accept: bool = True, feedback: str = "",
                decided_by: str = "user",
                project_name: str | None = None) -> dict[str, Any]:
    """Release a submitted plan, or send it back. Neo's path and the user's, shared.

    One function for both deciders on purpose: the escalation exists because Neo
    declined to take a decision, not because the decision changed shape, and two
    implementations of "release the plan" would be two chances to disagree about what
    releasing means.

    Releasing creates every child at once (`ProjectStore.create_plan_children`) and moves
    the feature order to `executing`; the ordinary claim-time dependency filter takes it
    from there, so no scheduler is added anywhere. Rejecting returns it to `planning` and
    delivers the reason to the planner as a message, which re-opens its existing session
    rather than starting a cold one.
    """
    from . import plans
    from .neo_store import NeoStore

    name, path, fo = find_feature_order(fo_id, project_name)
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
                manager=validation_enabled())
            manager = store.manager_work_order(fo_id)
            store.set_feature_status(fo_id, "executing")
            store.clear_feature_attention(fo_id)
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

    # Close the review question whichever way it went, so `jarvis neo list` stops
    # showing a decision that has been taken. Only if it is still open: Neo's own
    # verdicts are already recorded by the drain loop.
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
    # Only when one exists, so a release with validation off returns the dict it always
    # did — every caller reads this, including the CLI's JSON output.
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


def cancel_feature_order(fo_id: str, project_name: str | None = None) -> dict[str, Any]:
    """The user stopping a feature order, and everything it has running.

    A feature order that stopped while its planner and four children kept going would be
    a label, not a cancellation — so this reaches down. Every non-terminal work order it
    owns (the planner included) is cancelled through the ordinary `cancel` path, which is
    what stops the sessions; nothing here reimplements that.
    """
    name, path, fo = find_feature_order(fo_id, project_name)
    if fo["status"] in FO_TERMINAL_STATUSES:
        raise OpsError(f"{fo_id} is already {fo['status']}")
    store = ProjectStore(path)
    try:
        owned = store.feature_children(fo_id)
        if fo.get("plan_wo_id"):
            try:
                owned.append(store.get_work_order(fo["plan_wo_id"]))
            except KeyError:
                pass
        # The manager is not a child (`feature_children` filters to `kind='worker'`, and
        # that filter is what keeps it from deadlocking feature completion), so it has to
        # be reached explicitly — exactly like the planner above. A cancelled feature
        # whose manager kept a session open would be a label, not a cancellation.
        manager = store.manager_work_order(fo_id)
        if manager:
            owned.append(manager)
        stop_ids = [w["id"] for w in owned if w["status"] in OPEN_STATUSES]
        store.set_feature_status(fo_id, "cancelled")
        store.clear_feature_attention(fo_id)
    finally:
        store.close()
    for wo_id in stop_ids:
        cancel(wo_id)
    return {"project": name, "fo_id": fo_id, "title": fo["title"],
            "status": "cancelled", "cancelled_work_orders": stop_ids}


# -- Neo (OS answerer agent) ---------------------------------------------------------------------

#: The most of a referenced section that rides to Neo. A section this long is a design
#: document wearing one heading; the cut is announced in the context, never silent.
SECTION_SNAPSHOT_CHARS = 6000


def _resolve_section(path: Path, wo: dict[str, Any], ref_path: str,
                     which: str) -> str | None:
    """The referenced section's text, read from wherever this worker can see the file.

    Tried in the order the file is most likely to be authoritative: the worker's own
    worktree, the project tree, then the materialised feature snapshot under
    `.jarvis/features/` (where dispatch puts a parent feature's design document).
    """
    from . import sections

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
    for candidate in candidates:
        if candidate.is_file():
            section = sections.extract_section(candidate.read_text(), which)
            if section is not None:
                if len(section) > SECTION_SNAPSHOT_CHARS:
                    section = (section[:SECTION_SNAPSHOT_CHARS]
                               + "\n[… section truncated — it is longer than "
                                 f"{SECTION_SNAPSHOT_CHARS} characters]")
                return section
    return None


def ask_question(wo_id: str, question: str, project_name: str | None = None) -> dict[str, Any]:
    """(Workers) queue a question for Neo instead of stalling on the user.

    The work order flips to waiting_input WITHOUT flagging user attention — Neo
    exists precisely to keep these off the user's plate. The answer arrives as the
    worker's next user turn via the normal message-delivery path.

    A question is one paragraph that may reference a design artifact section in-text
    (`from section 3 of design doc "docs/specs/x.md"`). The reference is resolved HERE,
    at ask time: the section — and only the section — is snapshotted into the question's
    context, so Neo reads exactly the design context the paragraph argues from while the
    recorded question stays a paragraph.
    """
    from . import sections
    from .neo_store import NeoStore

    if len(question) > QUESTION_MAX_CHARS:
        raise OpsError(
            f"that question is {len(question)} characters; the cap is "
            f"{QUESTION_MAX_CHARS}. A question to Neo is one paragraph — the decision, "
            f"the options, your recommendation — arguing from a design artifact it "
            f"references in-text, e.g. `from section 3 of design doc "
            f"\"docs/specs/feature.md\": …`. The referenced section is delivered to "
            f"whoever answers, alongside your paragraph; you do not need to paste it."
        )

    name, path, wo = find_work_order(wo_id, project_name)
    context = f"{wo['title']}\n{(wo.get('description') or '')[:800]}"
    for ref_path, which in sections.find_refs(question):
        section = _resolve_section(path, wo, ref_path, which)
        if section is not None:
            context += f"\n\nReferenced artifact — {ref_path} § {which}:\n{section}"
        else:
            context += (f"\n\n(the question references {ref_path!r} section {which!r}, "
                        f"which could not be resolved — no such file or section)")
    neo = NeoStore()
    try:
        q = neo.ask(name, wo_id, question, context=context)
    finally:
        neo.close()
    store = ProjectStore(path)
    try:
        # The text, not just the id: the question lives in Neo's separate DB, so a
        # timeline built from the project store alone could never show what was asked.
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
            f"pasting context (the cap is {QUESTION_MAX_CHARS})"
        )
    return out


def neo_question_texts(wo_id: str) -> dict[int, str]:
    """{question_id: question} for back-filling a timeline. Never raises.

    `question_asked` events written before the text was stored in their payload carry
    only the id, and the text is in Neo's DB. Every surface that renders a timeline
    passes this in so those older entries still read as questions. Failure to open
    Neo's DB must not take `jarvis wo show` (or the work order page) down with it —
    a timeline missing one detail beats no timeline at all.
    """
    from .neo_store import NeoStore

    try:
        neo = NeoStore()
    except Exception:  # noqa: BLE001 — best-effort enrichment, see docstring
        return {}
    try:
        return neo.question_texts(wo_id)
    except Exception:  # noqa: BLE001
        return {}
    finally:
        neo.close()


def neo_status() -> dict[str, Any]:
    from .neo_store import NeoStore
    neo = NeoStore()
    try:
        return neo.counts()
    finally:
        neo.close()


def neo_export() -> dict[str, list[dict[str, Any]]]:
    """Neo's whole ledger as one stable document — see `NeoStore.export`.

    No filters and no truncation: this is the export path, not a listing.
    """
    from .neo_store import NeoStore

    neo = NeoStore()
    try:
        return neo.export()
    finally:
        neo.close()


def validate_seat(seat: str) -> None:
    """Refuse a seat name the panel does not have, BEFORE anything is written.

    An unknown seat is a typo, and a typo that is accepted writes a learning into a
    prefix no seat will ever read — invisible, and indistinguishable from the lesson
    having been lost.
    """
    from .neo_store import SEATS

    if seat and seat not in SEATS:
        raise OpsError(f"unknown panel seat {seat!r} — the seats are: {', '.join(SEATS)}")


def neo_review(question_id: int, approved: bool, feedback: str = "",
               seat: str = "") -> dict[str, Any]:
    """Review one of Neo's answers. A correction becomes a learning (Neo's own DB)
    and, when the work order is still open, is forwarded to the worker as guidance.

    `seat` routes that learning to one panel seat's prompt prefix instead of to every
    seat, so a correction teaches the seat that got this decision wrong. It is REFUSED
    unless that seat actually opined on this question: a correction aimed at a seat
    which never saw the question teaches the wrong reader, and the ledger acquires a
    lesson nobody can act on. That covers two cases — no panel ran at all (Neo answered
    single-agent), and a panel that ran without this seat (the fast route runs `premise`
    alone, which the design expects to be the common case).
    """
    from . import neo as neo_mod
    from .neo_store import NeoStore

    # Every refusal below happens before the first write: a rejected review must leave
    # the question unreviewed and the ledger untouched, not half-applied.
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
                seat=seat,
            )
    finally:
        neo.close()
    forwarded = False
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


def neo_answer_escalated(question_id: int, answer: str) -> dict[str, Any]:
    """The user answers a question Neo escalated; the answer flows to the worker
    through the same delivery path Neo's answers use."""
    from .neo_store import NeoStore

    neo = NeoStore()
    try:
        q = neo.get(question_id)
        if q is None:
            raise OpsError(f"neo question {question_id} not found")
        if q["status"] not in ("escalated", "failed", "queued"):
            raise OpsError(f"neo question {question_id} is {q['status']} — "
                           "only escalated/failed/queued questions take a user answer")
        neo.record_answer(question_id, answer, answered_by="user")
        neo.review(question_id, approved=True)  # user-authored ⇒ nothing to review
    finally:
        neo.close()
    delivery = send_message(q["wo_id"], f"[Answer from the user] {answer}",
                            project_name=q["project"])
    # The escalation is handled — release the work order from the attention list, AND
    # from the status that put it there. Clearing the flag alone lasted exactly one
    # reconcile tick: `true_blockers` derives the flag from `waiting_input`, so the
    # answered work order asked for the user again seconds later (GitHub issue 100).
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


# -- gates (privileged-action approvals) --------------------------------------------------------

def _project_gate_config(project_name: str):
    """The project's gate config from the catalog, or an empty one.

    An unreadable catalog yields "no gates", which fails closed for `jarvis gate
    request`: with no gate enabled there is nothing to request, and the caller is told
    so rather than getting a request nobody will ever act on.
    """
    from .gates import GateConfig

    try:
        catalog = resolve_catalog()
    except (OpsError, CatalogError):
        return GateConfig()
    for spec in catalog.projects:
        if spec.name == project_name:
            return spec.gates
    return GateConfig()


def request_gate_approval(wo_id: str, command: str, why: str = "", evidence: str = "",
                          project_name: str | None = None) -> dict[str, Any]:
    """(Workers) ask for permission to run a privileged command, making the case for it.

    The gate fires either way — running the command directly files a request too — but
    this route carries a justification and evidence, and the reviewer sees only what is
    written here. A worker that asks first is far more likely to be approved.
    """
    from . import gates
    from .neo_store import NeoStore

    name, path, wo = find_work_order(wo_id, project_name)
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
        if existing and existing["status"] == "pending":
            return {"project": name, "wo_id": wo_id, "approval_id": existing["id"],
                    "kind": action.kind, "status": "pending",
                    "note": "an identical request is already under review — end your "
                            "turn; the verdict arrives as your next user turn"}
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


def decide_gate(approval_id: int, verdict: str, reason: str = "",
                project_name: str | None = None) -> dict[str, Any]:
    """(User) rule on a gate directly, whatever Neo did or didn't say.

    `verdict` is `approved`, `denied` or `dismissed`. Also the resolution path for an
    escalation: Neo declining leaves the request pending precisely so this can close it.
    """
    from . import gates

    from .neo_store import NeoStore

    if verdict not in gates.VERDICTS:
        raise OpsError(f"unknown verdict {verdict!r} — expected one of "
                       f"{list(gates.VERDICTS)}")
    # A denial needs a reason because the worker has to act on it. A dismissal needs one
    # for a different reason: the text IS the defect report on the recogniser, and it is
    # the only record of what went wrong that anyone reading the false-positive count
    # will ever be able to inspect.
    if verdict == "denied" and not reason.strip():
        raise OpsError("a denial needs a reason — the worker acts on it")
    if verdict == "dismissed" and not reason.strip():
        raise OpsError("a dismissal needs a reason — it is the report on what the gate's "
                       "recogniser got wrong, and the only note attached to the "
                       "false-positive count")

    name, path, approval = _find_approval(approval_id, project_name)
    if approval["status"] != "pending":
        raise OpsError(
            f"approval {approval_id} is already {approval['status']}"
            + (f" (by {approval['decided_by']})" if approval["decided_by"] else "")
        )
    store = ProjectStore(path)
    central = CentralStore()
    try:
        gates.apply_decision(store, approval_id, verdict=verdict,
                             reason=reason or "approved by the user", decided_by="user",
                             central=central, project=name)
        store.clear_attention(approval["wo_id"])
    finally:
        central.close()
        store.close()
    # The user has decided, so Neo's queued question (if it is still waiting) is moot.
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
    """Locate an approval by id across registered projects.

    Approval ids are per-project autoincrements, so two projects can hold the same id.
    Ambiguity is reported rather than guessed at — silently opening the wrong project's
    release gate is not an acceptable failure mode.
    """
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
    """Approval requests across the fleet, newest first.

    `include_request` attaches each row's `neo_question` — the text the reviewer
    actually read. A reviewer deciding from a list needs the same page the first
    reviewer had; without it the dashboard would ask the user to approve a bare
    command string. One NeoStore is opened for the whole list, so this is cheap
    enough to render a page from.
    """
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
                wo_id, statuses=("pending",) if pending_only else None
            )
        finally:
            store.close()
        out.extend({**r, "project": name} for r in rows)
    out.sort(key=lambda r: r["ts"], reverse=True)
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


def list_gate_rules(role: str | None = None, kind: str | None = None,
                    include_retired: bool = False) -> dict[str, Any]:
    """The rule base: what the OS believes is privileged, and what it has learned is not.

    Returned with the canary report attached, because the two are only meaningful
    together. "Fourteen exemptions" is a number nobody can act on; "fourteen exemptions
    and every command that must gate still gates" is the claim the user is actually
    owed.
    """
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
    }


def retract_gate_rule(rule_id: str, reason: str) -> dict[str, Any]:
    """Retire a rule the user has overruled.

    Retracting an EXEMPTION re-arms a gate, and needs no further thought. Retracting a
    RECOGNISER disarms one, so the canary report is re-run afterwards and returned: if
    the removal left a command that must gate ungated, the user finds out in the same
    breath rather than the next time something ships unreviewed.
    """
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


def explain_gate(command: str, project_name: str | None = None) -> dict[str, Any]:
    """Why this command would, or would not, trip a gate.

    The diagnostic that a false positive used to require reading source code to get. A
    gate record holds the exact string that fired, so pasting it here is a mechanical
    two-minute answer to "why was this blocked" — which is the difference between
    reporting a classifier defect and guessing at one.
    """
    from .gate_rules import (
        KIND_NAMES,
        RuleSet,
        command_names,
        reads_only,
        scannable,
        shape_of,
    )

    # Without a project, every gate is treated as live: the question being asked is what
    # the RULES say, and answering it against an empty enabled-set would return "nothing
    # fires" for a command that fires four gates in any project that has them on.
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
    return out


def show_gate(approval_id: int, project_name: str | None = None) -> dict[str, Any]:
    """One approval request in full, including the text the reviewer saw."""
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


# -- deferral ------------------------------------------------------------------------------------

def defer(wo_id: str, title: str, why: str, description: str = "",
          neo_question_id: int | None = None,
          project_name: str | None = None) -> dict[str, Any]:
    """(Workers) hand work found on the way to whoever owns deciding about it.

    ONE post, and then it returns. Read the list of things this deliberately does NOT do,
    because every one of them is a thing it would be natural to add and each would break
    the same rule:

    * it does not look at `parent_id` to see whether this work order has a feature;
    * it does not look for a project manager;
    * it does not call `CentralStore.add_backlog`;
    * it does not name a work order as the recipient.

    A sender that asked "does the recipient exist?" would be a sender coupled to its
    recipient, and it would have to be edited again the day a second kind of recipient
    appears. `bus.deliver` owns all of that: it reaches the manager if the feature has
    one, and files the backlog item itself if not — which is the overwhelmingly common
    case and is exactly today's behaviour.

    The return value says the deferral was submitted and deliberately does not say what
    happened to it. The worker must not depend on the outcome, so it is not told one.
    """
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
        # On the work order's own record, because nobody reads worker transcripts and a
        # deferral is a decision about scope: the timeline is where the user finds out
        # this work order decided something was not its job.
        store.add_event(wo_id, "deferral_submitted",
                        {"title": title, "why": why, "envelope_id": env_id,
                         "neo_question_id": neo_question_id})
    finally:
        store.close()
    return {"project": name, "wo_id": wo_id, "envelope_id": env_id, "title": title,
            "note": "deferral submitted — it is out of your hands now; carry on with "
                    "your work order"}


# -- backlog ------------------------------------------------------------------------------------

def promote_backlog(item_id: str, force: bool = False,
                    as_feature: bool = False,
                    max_parallel: int | None = None) -> dict[str, Any]:
    """Turn an intake item into committed work.

    `as_feature` is the whole of the backlog's involvement with feature orders, and the
    backlog is deliberately left alone otherwise: it stays an OS-wide intake list of
    things that are not yet anybody's work, and a feature order is committed work. The
    only thing that changes is which of the two a promotion produces.
    """
    if max_parallel is not None and not as_feature:
        # Refused rather than ignored: a work order has no children to cap, so silently
        # dropping the flag would promote something other than what was asked for.
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
            # `promoted_wo_id` takes the feature order's id: the column records what the
            # item BECAME, and widening it to a second nullable column would leave every
            # reader having to check both to answer one question.
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


# -- token accounting ----------------------------------------------------------------------------

#: The turn states whose spend is final. A running turn has no result JSON yet, so it
#: can be listed but never counted.
_SETTLED_TURN_STATES = ("done", "failed")


def _turn_usage(store: ProjectStore, turn: dict[str, Any]) -> dict[str, Any] | None:
    """A settled turn's recorded usage envelope, lazily (re-)derived from its outfile.

    Two migrations run through this one seam, and both are lazy for the same reason —
    the outfile is still on disk for the overwhelming majority of turns, so history is
    recoverable on demand and, once written back, outlives the file:

    * Rows reaped before `usage_json` existed carry NULL.
    * Rows written before `USAGE_SCHEMA_VERSION` 2 carry token totals read from the
      result envelope's top-level `usage` object, which counts a fraction of the turn
      (see `claude_cli.derive_turn_usage`). They are re-derived rather than trusted: a
      table holding two incompatible counts in one column cannot be summed at all.

    A stale envelope whose outfile is gone is returned AS IT IS, still stamped with its
    old version, because a wrong number that says which reading produced it can be
    labelled on the page — Neo asked for exactly that on question 121 — while dropping
    it would report a turn that certainly cost something as having cost nothing.
    """
    from . import claude_cli

    raw = turn.get("usage_json")
    stored = db.from_json(raw, None) if raw else None
    if isinstance(stored, dict) and \
            stored.get("usage_v", 1) >= claude_cli.USAGE_SCHEMA_VERSION:
        return stored
    if turn.get("state") not in _SETTLED_TURN_STATES or not turn.get("outfile"):
        return stored
    result = claude_cli.read_turn_result(Path(turn["outfile"]))
    if result is None or not result.usage:
        return stored
    store.set_turn_usage(turn["id"], db.to_json(result.usage))
    return result.usage


def _turn_row(turn: dict[str, Any], u: dict[str, Any] | None) -> dict[str, Any]:
    """One turn, flattened for the per-turn table. `recorded` is the honesty bit."""
    duration = None
    if turn.get("ended_at") and turn.get("started_at"):
        duration = round(turn["ended_at"] - turn["started_at"], 1)
    row = {
        "seq": turn["seq"], "kind": turn["kind"], "state": turn["state"],
        "started_at": turn["started_at"], "ended_at": turn.get("ended_at"),
        "duration_s": duration, "cost_usd": turn.get("cost_usd"),
        "recorded": u is not None,
        # Which message set this turn going, where one did. It is the join that lets a
        # message on the work order page show what answering it cost.
        "msg_id": turn.get("msg_id"),
    }
    if u is None:
        return row
    window = u.get("context_window") or 0
    peak = u.get("context_peak") or 0
    row.update({
        "cost_usd": turn["cost_usd"] if turn.get("cost_usd") is not None
        else u.get("total_cost_usd"),
        # Which reading of the result envelope these numbers came from. A row still on
        # version 1 counted a fraction of its turn and could not be re-derived (its
        # outfile is gone), so every surface that shows it has to be able to say so.
        "usage_v": u.get("usage_v") or 1,
        # The turn's spend split by the model that served it — the CLI's own per-model
        # accounting, which is where the token totals now come from.
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
        # The /context statistic: how full the model's window was at this turn's
        # largest call. This is the column that shows a work order bloating.
        "context_pct": round(100 * peak / window, 1) if window else None,
        "duration_api_ms": u.get("duration_api_ms"),
    })
    return row


def _turn_rows(store: ProjectStore, wo_id: str) -> list[dict[str, Any]]:
    return [_turn_row(t, _turn_usage(store, t)) for t in store.list_turns(wo_id)]


def _turn_summary(rows: list[dict[str, Any]]) -> tuple[str, int, int,
                                                       dict[str, Any] | None]:
    """(provenance, recorded, settled, exact totals) over one work order's turns.

    Provenance is the label that keeps the two accounting systems from being silently
    mixed: `recorded` — every settled turn has the CLI's own numbers (exact);
    `mixed` — some do and some exist only in transcripts, so no single total is
    honest; `transcript` — nothing recorded, only the estimate (sessions predating
    turn capture, or never Jarvis-driven).
    """
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
            # Exact aggregation IS the sum of the turns — nothing is re-derived.
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
    """Split one work order's `agent_calls` groups into (the OS's, the worker's own).

    Both live in the same table because both are recorded the same way and for the same
    reason, and both belong to the same work order. They are reported apart because they
    are different SHAPES of spend: what Jarvis spent thinking about the order, and what
    the order's own worker spent one process further down its tree. A work order that
    ran an eval suite and one that asked Neo four questions have nothing in common, and
    a single column would say they did (issue #103).
    """
    from . import agent_usage

    os_side, worker_side = [], []
    for g in groups:
        target = worker_side if agent_usage.is_subprocess(g.get("kind") or "") else os_side
        target.append(g)
    return os_side, worker_side


def _os_spend(groups: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """One work order's OS-side spend, from its `agent_calls` groups.

    Two currencies, deliberately not blended into one field. `os_recorded_cost_usd` is
    the `claude` CLI's own figure summed — exact, and comparable with a work order's
    recorded turns. `os_cost_usd` re-prices the same tokens at Anthropic list prices, so
    it can be added to the transcript estimate the rest of this report is denominated in.
    Adding the exact figure to the estimate instead would produce a number that is
    neither, which is the mistake `_turn_summary`'s provenance label exists to prevent.

    Priced PER MODEL GROUP: the digest and the panel's seats routinely run on a cheaper
    model than Neo, and pricing the fleet's OS spend at one blended rate would make the
    cheap calls look expensive and hide the dear ones.
    """
    return _call_spend(groups, "os")


def _subproc_spend(groups: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """The same arithmetic, over the `claude` processes the WORKER spawned beneath itself.

    Same table, same pricing, its own prefix — the third class of spend on a work order,
    beside the worker's own conversation and Jarvis's overhead. What it counts is every
    descendant call that came through Jarvis's transport; a bare `claude -p` from a shell
    is invisible to it, which is why `cost_report` marks the whole report a floor.
    """
    return _call_spend(groups, "subproc")


def _priced_group(usage_mod: Any, g: dict[str, Any]) -> Any:
    """One `agent_call_totals` group at list prices, TTL SPLIT INCLUDED.

    Passing `cache_1h`/`cache_5m` is the whole point of the function: `usage.priced`
    falls back to the 1.25x floor without them, which under-priced every OS-side call
    that bought the one-hour cache. Spec: 2026-08-22-the-five-minute-write-everywhere.md.

    `"unknown"` rather than `""` for an uncaptured model: an empty model prices at ZERO
    in `usage.price_for` (that branch is for `<synthetic>`, never billed), so a real call
    would silently cost nothing. An unrecognised name falls through to the default rate.
    """
    return usage_mod.priced(
        g.get("model") or "unknown", input=g.get("input") or 0,
        cache_write=g.get("cache_write") or 0, cache_read=g.get("cache_read") or 0,
        output=g.get("output") or 0, messages=g.get("calls") or 0,
        cache_1h=g.get("cache_1h") or 0, cache_5m=g.get("cache_5m") or 0)


def _call_spend(groups: Sequence[dict[str, Any]], prefix: str) -> dict[str, Any]:
    """Sum and price one class of `agent_calls` groups under `<prefix>_…` keys.

    Shared by `_os_spend` and `_subproc_spend`: the two classes differ in what they mean
    and where they are shown, never in how a token is counted or priced, and two copies
    of this loop would be two places for those to drift apart.
    """
    from . import agent_usage
    from . import usage as usage_mod

    total = usage_mod.Usage()
    by_kind: dict[str, dict[str, Any]] = {}
    calls = failed = 0
    exact = 0.0
    for g in groups:
        u = _priced_group(usage_mod, g)
        total = total + u
        calls += g.get("calls") or 0
        failed += g.get("failed") or 0
        exact += g.get("cost_usd") or 0.0
        kind = g.get("kind") or "other"
        entry = by_kind.setdefault(kind, {"kind": kind, "label": agent_usage.describe(kind),
                                          "calls": 0, "cost_usd": 0.0,
                                          "billed_input": 0, "output": 0})
        entry["calls"] += g.get("calls") or 0
        entry["cost_usd"] = round(entry["cost_usd"] + u.list_cost_usd, 4)
        entry["billed_input"] += u.billed_input
        entry["output"] += u.output
    return {
        f"{prefix}_calls": calls,
        f"{prefix}_failed_calls": failed,
        f"{prefix}_cost_usd": round(total.list_cost_usd, 4),
        f"{prefix}_recorded_cost_usd": round(exact, 4),
        f"{prefix}_billed_input": total.billed_input,
        f"{prefix}_output": total.output,
        f"{prefix}_total_tokens": total.total_tokens,
        # Carried up for the footer's write-TTL line; see `_write_ttl`.
        f"{prefix}_cache_write": total.cache_write,
        f"{prefix}_cache_1h": total.cache_1h,
        f"{prefix}_cache_5m": total.cache_5m,
        # Dearest first: the whole point of the split is to say where the spend goes.
        f"{prefix}_by_kind": sorted(by_kind.values(), key=lambda k: -k["cost_usd"]),
    }


def _unit_row(name: str, wo: dict[str, Any], index: dict[str, list[Path]],
              turn_rows: Sequence[dict[str, Any]] = (),
              os_groups: Sequence[dict[str, Any]] = ()) -> dict[str, Any]:
    """One work order's spend, flattened for a table.

    The transcript figures stay the body of the row (they are the only source with a
    subagent split and the re-write tax); the recorded figures ride along with their
    provenance label so a reader can tell an exact number from an estimate.

    FOUR TOTALS, and the differences between them are the point. `list_cost_usd` is what
    the WORKER's own conversation cost. `os_cost_usd` is what Jarvis spent on this work
    order behind the worker's back — Neo answering it, the panel deliberating on it, the
    digest shortening it. `subproc_cost_usd` is what the worker spent BELOW itself, in
    `claude` processes its own tool calls spawned: an eval suite, a script, a nested
    harness. `total_cost_usd` is all three, and it is the number that answers "what did
    this work order cost". A reader who only ever sees the first one concludes the OS is
    free and that a work order which ran the eval suite twice was cheap.
    """
    from . import usage as usage_mod

    session = usage_mod.read_session(wo.get("session_id") or "", index=index)
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
        # A session's turn count is its resume boundaries plus the opening turn; see
        # `usage.Usage.resume_boundaries` for why the boundaries are counted that way.
        "turns": total.resume_boundaries + 1 if session.found else 0,
        "subagent_count": session.subagent_count,
        "subagent_cost_usd": round(session.subagents.list_cost_usd, 2),
        "provenance": provenance,
        "recorded_turns": recorded,
        "settled_turns": settled,
        "recorded_cost_usd": recorded_cost,
        **os_spend,
        **subproc_spend,
        # Estimate + estimate + estimate, all at list prices. A work order whose
        # transcript is gone still reports the recorded halves here: those were written
        # down by the OS itself and do not depend on a file Claude Code is free to prune.
        "total_cost_usd": round(worker_cost + os_spend["os_cost_usd"]
                                + subproc_spend["subproc_cost_usd"], 4),
        "total_recorded_cost_usd": round(recorded_cost
                                         + os_spend["os_recorded_cost_usd"]
                                         + subproc_spend["subproc_recorded_cost_usd"], 4),
        # Is there anything to show at all? `found` alone answered that before the OS's
        # own calls were counted, and would now hide a work order whose transcript is
        # pruned but which cost Neo five calls — or which spent its whole bill in
        # subprocesses.
        "measurable": bool(session.found or os_spend["os_calls"]
                           or subproc_spend["subproc_calls"] or recorded),
        **total.as_dict(),
    }


#: Why every figure in a cost report is a lower bound, said the same way in every
#: surface that renders one. Carried in the payload rather than written into the CLI so
#: the dashboard and any JSON consumer state it identically — a caveat that appears in
#: one renderer and not another is a caveat the reader learns to ignore.
COST_FLOOR_NOTE = (
    "a floor: `claude` processes a worker spawns outside Jarvis's own transport "
    "(a bare `claude -p` from a shell) leave nothing behind that names a work order"
)


def bill(target: str, project: str | None = None, *,
         live: bool = False) -> dict[str, Any]:
    """The itemised bill for one order — see `jarvis.bill`.

    `cost_report` answers "what did the fleet cost" and is the right shape for a
    listing; this answers "where did THIS order's tokens go" and is the right shape for
    a page you can expand. One thin wrapper rather than a second import path, so every
    surface — the CLI, the dashboard, anything later — reaches it the way it reaches
    everything else in the OS.

    A settled order's bill was sealed when it settled and comes back as it was sealed;
    `live=True` recomputes it from whatever survives today, which is what a test that
    compares the two needs and what nothing else should ask for.
    """
    from .bill import build

    return build(target, project, live=live)


def cost_report(project: str | None = None, target: str | None = None,
                limit: int = 50, include_hidden: bool = True) -> dict[str, Any]:
    """What the fleet's work has cost in tokens, read back from Claude Code's transcripts.

    `target` is a work-order or feature-order id for a single unit — a feature order
    rolls up its planner and every child, which is the only way to see what a planned
    feature actually cost. Otherwise this reports every work order that still has a
    transcript, dearest first.

    Hidden work orders are INCLUDED by default, unlike every other listing: hiding is a
    gesture about attention, and a hidden work order's tokens were spent just the same.
    A cost report that quietly omitted them would understate the bill in exactly the
    case where someone is trying to find out where the bill came from.

    The transcripts belong to Claude Code, not to Jarvis, and it prunes them on its own
    schedule. A work order whose transcript is gone reports `found: false` rather than
    zero: an unmeasurable cost and a zero cost are different answers, and rendering them
    the same would turn a gap in the evidence into a claim about the spend.

    The OS's OWN spend on each work order (`agent_calls` — Neo, the panel, the digest) is
    read alongside and reported both separately and in the total. It does not come from
    transcripts, so it survives pruning and is present even on a unit that reports
    `found: false`. So is the third class, `subproc_…`: the `claude` processes the WORKER
    spawned beneath itself, recorded the same way and kept apart because an eval suite and
    a Neo question are not the same kind of spending.

    EVERY FIGURE HERE IS A FLOOR, and the report says so rather than implying otherwise.
    A worker can reach the model without going through Jarvis's transport — a bare
    `claude -p` in a shell command — and such a call leaves nothing behind that names a
    work order. The marker is unconditional on purpose: a heuristic that tried to guess
    whether any escaped would be blind in exactly the cases it was meant to catch, and a
    flat statement is always true and costs one line (ruled on wo-76e021aa, issue #103).
    """
    from . import usage as usage_mod

    index = usage_mod.index_sessions()
    paths = registered_project_paths()
    if project and project not in paths:
        raise OpsError(f"project {project!r} not registered (known: {sorted(paths)})")
    if target:
        return _cost_for_target(target, project, index)

    os_groups = _os_groups(project)
    scope = {project: paths[project]} if project else paths
    units: list[dict[str, Any]] = []
    for name, path in sorted(scope.items()):
        if not path.is_dir():
            continue
        store = ProjectStore(path)
        try:
            for wo in store.list_work_orders(limit=limit, include_hidden=include_hidden):
                units.append(_unit_row(name, wo, index, _turn_rows(store, wo["id"]),
                                       os_groups.get(wo["id"], ())))
        finally:
            store.close()
    # Dearest first, counting what Jarvis spent on the order as part of what it cost —
    # otherwise a work order that asked Neo twenty questions sorts as though it were cheap.
    units.sort(key=lambda u: u["total_cost_usd"], reverse=True)
    return {"scope": project or "fleet", "units": units,
            **_rollup(units), "os_unattributed": _os_unattributed(os_groups),
            "floor": True, "floor_reason": COST_FLOOR_NOTE}


def _os_groups(project: str | None = None) -> dict[str, list[dict[str, Any]]]:
    """Every work order's OS-side call groups, in one query. See `_os_spend`.

    One read of `os.db` for the whole fleet rather than one per work order: this report
    already walks every work order there is, and the OS's calls are all in one table.
    """
    central = CentralStore()
    try:
        groups: dict[str, list[dict[str, Any]]] = {}
        for row in central.agent_call_totals(project):
            groups.setdefault(row["wo_id"] or "", []).append(row)
        return groups
    finally:
        central.close()


def _os_unattributed(groups: dict[str, list[dict[str, Any]]]) -> dict[str, Any]:
    """OS spend that no work order caused (`agent_calls.wo_id = ''`).

    Reported on its own line rather than folded into a work order or dropped. There is
    none of it today — every OS call the OS makes is made for a question, and a question
    always names a work order — but a total that silently omitted a future kind of
    overhead would be wrong in the direction nobody checks.
    """
    return _os_spend(groups.get("", []))


def _rollup(units: list[dict[str, Any]]) -> dict[str, Any]:
    """Totals over the units that could actually be measured.

    Unmeasured units are counted separately rather than summed as zero, so the report
    can say how much of the fleet its total is speaking for.
    """
    measured = [u for u in units if u["found"]]
    worker_cost = round(sum(u["list_cost_usd"] for u in measured), 2)
    # Over ALL units, not just measured ones, for the same reason `recorded_cost_usd` is:
    # `agent_calls` is the OS's own record and does not depend on a transcript surviving.
    os_cost = round(sum(u.get("os_cost_usd") or 0 for u in units), 2)
    subproc_cost = round(sum(u.get("subproc_cost_usd") or 0 for u in units), 2)
    return {
        "measured": len(measured),
        "unmeasured": len(units) - len(measured),
        "totals": {
            # Over ALL units, not just measured ones: the recorded figure comes from
            # the OS's own turn rows, which survive transcript pruning — that
            # independence is the point of recording.
            "recorded_cost_usd": round(
                sum(u.get("recorded_cost_usd") or 0 for u in units), 2),
            "list_cost_usd": worker_cost,
            # The headline: workers, everything the OS spent on their behalf, and
            # everything they spent below themselves.
            "total_cost_usd": round(worker_cost + os_cost + subproc_cost, 2),
            "os_cost_usd": os_cost,
            "os_recorded_cost_usd": round(
                sum(u.get("os_recorded_cost_usd") or 0 for u in units), 2),
            "os_calls": sum(u.get("os_calls") or 0 for u in units),
            "os_billed_input": sum(u.get("os_billed_input") or 0 for u in units),
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
            **_write_ttl(measured, units),
        },
    }


def _write_ttl(measured: list[dict[str, Any]], units: list[dict[str, Any]]
               ) -> dict[str, int]:
    """Cache-write tokens and their TTL split, over every class of spend on the report.

    One figure for worker and OS spend together: the two were switched to the 5-minute
    write ten days apart, so a total speaking for one of them would read as all-clear
    while half the bill was still at 2x (spec:
    2026-08-22-the-five-minute-write-everywhere.md).

    `measured` for the transcript half (a unit whose transcript is gone has no split to
    contribute), `units` for the recorded halves, which survive transcript pruning — the
    same asymmetry `_rollup` applies to every other figure it sums.
    """
    out = {"cache_write": 0, "cache_1h": 0, "cache_5m": 0}
    for key in out:
        out[key] = (sum(u.get(key) or 0 for u in measured)
                    + sum((u.get(f"os_{key}") or 0) + (u.get(f"subproc_{key}") or 0)
                          for u in units))
    return out


def _cost_for_target(target: str, project: str | None,
                     index: dict[str, list[Path]]) -> dict[str, Any]:
    """One work order, or a feature order rolled up over its planner and children.

    The feature order is tried FIRST. A feature order and a work order cannot share an
    id, but `find_work_order` raises the more familiar error, and resolving the work
    order first would report a planner's own spend under the feature order's id — the
    one number a reader of `jarvis cost fo-…` is least likely to want.
    """
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
        # The per-turn breakdown is the single-work-order payload: it is what shows
        # WHERE in a bloated work order the cost rose, turn by turn. `os_calls_detail`
        # is its counterpart for the other half of the bill — call by call, so a work
        # order that cost four rounds of Neo says so. `subproc_detail` is the third:
        # grouped rather than listed, because one `pytest evals/llm` is forty calls and
        # "pytest: 40 calls, $3.10" is the fact, not forty near-identical rows.
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
                     for child in store.feature_children(fo["id"]))
    finally:
        store.close()
    return {"scope": fo["id"], "title": fo["title"], "status": fo["status"],
            "units": units, **_rollup(units),
            "floor": True, "floor_reason": COST_FLOOR_NOTE}


def _os_calls_detail(wo_id: str, limit: int = 200) -> list[dict[str, Any]]:
    """Every OS call made for one work order, newest first, priced at list.

    Flattened for a table the same way `_turn_row` flattens a turn, and priced in the
    report's own currency so a reader can compare a Neo answer with a worker turn without
    doing the arithmetic in their head. `cost_usd` stays the CLI's exact figure beside it.

    The worker's OWN subprocess calls are filtered out and summarised by `_subproc_detail`
    instead. One row per call is the right shape for five panel seats and the wrong one
    for an eval suite, which would bury them under two hundred lines saying `pytest`.
    """
    from . import agent_usage
    from . import usage as usage_mod

    central = CentralStore()
    try:
        rows = central.agent_calls(wo_id=wo_id, limit=limit)
    finally:
        central.close()
    out = []
    for row in rows:
        if agent_usage.is_subprocess(row["kind"]):
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
        })
    return out


def _subproc_detail(groups: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    """What the worker spent below itself, by what ran it, dearest first.

    Grouped rather than listed because of the shape of what is being counted: an OS call
    is a deliberate, countable act (five seats on one gate review) and a subprocess call
    comes in batches — one `pytest evals/llm` is forty. The label is the program name
    `claude_cli._attribute_subprocess` captured, so a line reads "pytest: 40 calls,
    ~$3.10", which is the sentence a reader of an expensive work order actually wants.

    Built from the groups `agent_call_totals` already summed in SQL rather than from
    rows, so there is no limit to truncate against and no second query.
    """
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
