"""High-level operations shared by the CLI, the web UI, and the Jarvis persona.

Every mutation of the OS goes through here, so all surfaces behave identically.
"""

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

log = logging.getLogger("jarvis.ops")

if TYPE_CHECKING:  # pragma: no cover - typing only
    from . import landing

from .bootstrap import BootstrapReport, bootstrap_project, settings_drift
from .catalog import (
    DEFAULT_VALIDATION_FOLLOW_UP_CAP,
    SAFETY_KEYS,
    Catalog,
    CatalogError,
    ProjectSpec,
    load_catalog,
    parse_catalog,
    worker_stalls_on_prompts,
)
from . import budget, bus, config_version, db, fleet, invariants
from .sections import QUESTION_MAX_CHARS, QUESTION_WARN_CHARS
from .central_store import CentralStore
from .daemon import daemon_running
from .github import GitHubError
from .invariants import PR_CLOSED_BLOCKER, UNLANDED_BLOCKER, true_blockers
from .paths import daemon_pidfile, ensure_home, logs_dir
from .project_store import (
    ASSUMPTION_DECIDER_OS,
    ASSUMPTION_DECIDER_USER,
    FO_OPEN_STATUSES,
    FO_TERMINAL_STATUSES,
    NO_TURN,
    OPEN_STATUSES,
    FORCEABLE_STATUSES,
    OPEN_VALIDATION_OUTCOMES,
    TERMINAL_STATUSES,
    ProjectStore,
    feature_status_label,
    is_feature_order_id,
    validation_standing,
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


def validation_enabled(project: str | None = None) -> bool:
    """Is validation on — for `project`, or fleet-wide? False if the catalog can't be read.

    The validation layer ships DISABLED, and at that default the OS must behave exactly
    as it does today. A catalog that has moved, been deleted or was never registered is
    therefore answered `False` rather than raised: the alternative is a release path that
    works today and fails tomorrow for a reason that has nothing to do with the plan being
    released. The catalog file is read on demand rather than cached because the daemon may
    be days old and the answer is only ever consulted at the moments a unit changes shape.
    """
    cfg = validation_config(project)
    return bool(cfg is not None and cfg.enabled)


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
               catalog_path: str | None = None,
               include_os: bool = True) -> dict[str, Any]:
    """Run the OS's post-condition checks over one project or the whole fleet.

    Read-only unless `repair` is set, so it is safe to run at any time. The daemon runs
    the same checks with repair enabled on every reconcile tick — this is the manual
    handle for "is the OS lying to me right now?".

    It runs MORE than the daemon does on most ticks: `invariants.SLOW_INVARIANTS` walk
    every completed order a project has ever had, so the daemon rations them to its own
    cadence and this does not. INV-WORK-LANDED is the one that matters — "what has this
    fleet delivered that nobody merged" has no other home, and the audit that first
    answered it (GitHub issue #232, six stranded work orders) was a one-off done by hand.

    `repair` no longer changes that answer. It used to: the check kept a cache of settled
    verdicts as a timeline event and refreshed the default branch it measured against,
    and a read-only run could do neither, so a plain `jarvis doctor` paid a per-file git
    walk every time AND withheld the content verdict. Since the check began judging the
    PULL REQUEST it is a pure timeline read with nothing to write and no ref to be stale
    — what it cannot do is ASK GITHUB, and that round trip is the daemon's
    (`Daemon.refresh_landings`). So a plain run and a repairing one say the same
    thing. On a project the daemon has never swept both say the same thing too, and it is
    not silence: `INV-LANDING-AUDIT-FRESH` names how many pull requests the audit has no
    current answer for. Until review round 1 of wo-16a488ee it WAS silence, so this
    command printed "all OS invariants hold" over an audit that had no data — a lie that
    is worse than any false positive, because nothing shows it happening.

    `include_os=False` drops the OS-LEVEL checks — `check_os` and the release marker —
    and keeps the per-project ones. The scheduler's daily run passes it for every project
    but the one that owns the install: those checks are about the OS, not about any
    project, so a fleet of six would otherwise report one broken dashboard six times
    every morning. Interactive `jarvis doctor` leaves it on, which is why the default is
    True: a human who typed the command is asking about everything they can see.
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
            # `slow=True` unconditionally: INV-WORK-LANDED is the whole reason issue
            # #232 asked for a report, and a human who typed `jarvis doctor` is waiting
            # for its answer. The daemon is the caller that has to ration it — it walks
            # the whole settled backlog, which no other check here does.
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

    `approval`, `plan`, `alarm` and `assumption` questions are dropped here rather than at
    the display: each is reported by the thing that actually carries the decision (the
    gate item, the feature order, the alarm, the work order's own "assumptions pending
    review" flag), and telling the user to `jarvis neo answer` a question whose real
    resolution is `jarvis gate approve` — or `jarvis wo review` — sends them to the wrong
    command.
    """
    from .neo_store import NeoStore

    neo = NeoStore()
    try:
        return (neo.counts(),
                [q for q in neo.list_questions(statuses=("escalated", "failed"))
                 if q.get("kind") not in ("approval", "plan", "alarm", "assumption")])
    finally:
        neo.close()


def _held_jobs(store: ProjectStore, catalog: Catalog | None = None) -> list[dict[str, Any]]:
    """Scheduled jobs this project is STILL SUPPOSED TO RUN and cannot.

    Only the held ones: a scheduler ticking along is not news, and a line per job per
    project on every `jarvis status` would spend exactly the attention budget the
    scheduler exists to protect.

    Filtered by the config for `invariants.check_schedule_progresses`' reason — a hold
    survives being switched off, because only a firing clears it, and a project that has
    been told to stop scheduling will never reach one. Resolved through
    `schedule_config_at`, the SAME call that invariant makes, so the two cannot answer
    differently for one project.
    """
    cfg = schedule_config_at(store.project_path, catalog)
    if not cfg.enabled:
        return []
    return [{"job_id": st["job_id"], "since": st["held_since"],
             "reason": st["held_reason"] or "", "wo_id": st["last_wo_id"]}
            for st in store.list_schedule_states()
            if st["held_since"] and st["job_id"] in cfg.jobs]


def os_status(catalog: Catalog | None = None) -> dict[str, Any]:
    central = CentralStore()
    try:
        pid = daemon_running()
        projects = []
        attention: list[dict[str, Any]] = []
        # Two things off the catalog. A best-effort map of each project's worker
        # permission mode, to catch a fleet misconfigured into a mode that stalls
        # background workers (see below) — and the ACCOUNT's own state: how many worker
        # turns are in flight against `os.defaults.max_in_flight`, and whether Claude is
        # refusing them (src/jarvis/fleet.py). The second is what a `pending` work order
        # that is not starting can be waiting for, and no per-project read can see it.
        fleet_state = None
        try:
            _cat = catalog or resolve_catalog()
            mode_by_project = {ps.name: ps.worker.permission_mode for ps in _cat.projects}
            fleet_state = fleet.current(_cat)
        except (OpsError, CatalogError):
            mode_by_project = {}
            # `_cat` stays None and `_held_jobs` resolves the catalog itself, landing on
            # `schedule_config_at`'s single fallback. Deliberately NOT a second map here:
            # a name-keyed one beside the invariant's path-keyed lookup is how the two
            # surfaces came to disagree about the same project in the first place.
            _cat = None
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
                # Derived once for the whole strip: a flagged order can be parked AND
                # owe the user something else, and `true_blockers` deliberately appends
                # the parking behind the decision rather than over it, so the flag alone
                # cannot carry both (invariants.parked_reason).
                parked_by_id = {}
                for wo in flagged.values():
                    reason = invariants.parked_reason(store, wo)
                    if reason:
                        parked_by_id[wo["id"]] = reason
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
                    # …and the half the flag could not carry. Optional the way `attach`
                    # and `resume_auto` are: present only where it says something the
                    # reason does not.
                    parked = parked_by_id.get(wo["id"])
                    if parked and parked != wo["attention_reason"]:
                        item["parked"] = parked
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
                            + ", ".join(_child_note(k, parked_by_id) for k in kids)
                        )
                    progress = feature_progress(store, fo)
                    # §6.2 of docs/superpowers/specs/2026-09-23-improvement-orders.md: an
                    # improvement order's reason is `findings.review_headline` word for
                    # word — it already leads with counts and already names the command,
                    # and "0/1 done" about a one-child family displaces those counts with
                    # noise. Feature orders keep the prefix and `jarvis fo show`.
                    improvement = (fo.get("kind") or "feature") == "improvement"
                    body = "; ".join(reasons)
                    attention.append({
                        "project": p["name"], "wo_id": None, "fo_id": fo_id,
                        "title": fo["title"],
                        # Kind-aware through the ONE mapping in project_store — §2.2 of
                        # docs/superpowers/specs/2026-09-23-improvement-orders.md. A
                        # feature order's label is unchanged by construction.
                        "status": (f"{fo.get('kind') or 'feature'}:"
                                   f"{feature_status_label(fo.get('kind'), fo['status'])}"),
                        "reason": body if improvement
                                  else f"{progress['label']} — {body}",
                        "rolled_up": [k["id"] for k in kids],
                        "decide": (f"jarvis io review {fo_id}" if improvement
                                   else f"jarvis fo show {fo_id}"),
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
                         "pause": invariants.pause_note(store, wo),
                         # And the whole answer in one string, fleet state included, so
                         # `jarvis status` prints what the dashboard shows rather than a
                         # bare status word.
                         "status_label": invariants.status_label(store, wo, fleet_state)}
                        for wo in open_wos
                    ],
                    "settings_drift": drift,
                    # ONLY THE HELD ONES. A scheduler that is ticking along is not news
                    # and adding a line per job per project to every `jarvis status`
                    # would spend exactly the attention budget the scheduler is designed
                    # to protect; a job that has wanted to fire and could not is the one
                    # state where silence and death look the same from outside.
                    # INV-SCHEDULE-HELD is the louder half, once it has been held for
                    # days — this is what answers "why has there been no doctor order
                    # since Tuesday" before then.
                    "schedule_held": _held_jobs(store, _cat),
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
            # A `triage` question is the one kind with NO WORKER BEHIND IT (issue #240):
            # nothing is blocked on the answer, the bug is sitting on the tracker, and
            # `jarvis neo answer` would try to message a work order that does not exist.
            # Still listed — an unconfirmed `blocker` the user never hears about is the
            # failure this whole path exists to avoid — but pointed at the command that
            # actually resolves it.
            triage = q.get("kind") == "triage"
            url = ""
            if triage:
                from . import issues
                url = (issues.triage_payload(q) or {}).get("issue_url") or ""
            attention.append({
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
        # Gates Neo sent up. These are the only approval requests that cost the user
        # anything: the rest were decided without them, which is the point.
        gate_items = []
        # Gate requests that turned out not to be gated actions at all. Reported as a
        # number and never as an attention item: a classifier defect is the OS's problem,
        # not the user's, but the rate is the one signal that says whether the
        # recognisers are getting better.
        false_positives = 0
        # Held requests whose worker never came back — evidence about the same classifier,
        # counted separately because an abandonment is not a verdict (spec 2026-09-12 §5).
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
                      "false_positives": false_positives,
                      "abandoned": abandoned},
            "healthy": pid is not None and not attention,
        }
    finally:
        central.close()


# -- work orders -----------------------------------------------------------------------------

def _child_note(wo: dict[str, Any], parked: dict[str, str]) -> str:
    """One rolled-up child on a feature order's attention line.

    Its own reason, plus the parking when the reason does not already say it. The rollup
    is what a feature's children get INSTEAD of a line each, so this is the only place
    `jarvis status` can name a parked child — which is the shape wo-a4bd6958 was.
    """
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


def create_work_order(project_name: str, title: str, description: str = "",
                      origin: str = "jarvis", model: str | None = None,
                      effort: str | None = None, permission_mode: str | None = None,
                      append_system_prompt: str | None = None,
                      backlog_id: str | None = None,
                      depends_on: list[str] | None = None,
                      parent_id: str | None = None,
                      issue_url: str | None = None,
                      issue_priority: str | None = None,
                      budget_usd: float | None = None,
                      metadata: dict[str, Any] | None = None) -> dict[str, Any]:
    """File a work order. `parent_id` files it UNDER a feature order.

    `budget_usd` caps what the order may spend; None falls back to the project's catalog
    default, and None again — the fleet default — means no ceiling at all, which is what
    every work order had before budgets existed. Resolved HERE, at creation, so `jarvis
    wo show` can state the number and a later catalog edit cannot move a ceiling the user
    has already been told about (`budget.default_for`).

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
        wo = store.create_work_order(
            title=title, description=description, origin=origin, model=model,
            effort=effort, permission_mode=permission_mode,
            append_system_prompt=append_system_prompt, backlog_id=backlog_id,
            depends_on=depends_on, parent_id=parent_id, issue_url=issue_url,
            issue_priority=issue_priority,
            # AT CREATION, never a follow-up write: the daemon can claim and dispatch a
            # new work order before a second statement would land, and a worker that
            # cannot read where it came from is why `origin_io` exists (§5.3.1).
            metadata=metadata,
            budget_usd=(budget_usd if budget_usd is not None
                        else budget.default_for(_spec_or_none(project_name))),
        )
        # AT CREATION, from the brief the order is actually given — the only moment at
        # which "this order points at that issue" is a fact rather than an inference.
        record_issue_references(store, paths[project_name], wo)
        return wo
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


def fleet_if_held(wos: Iterable[dict[str, Any]]) -> "fleet.Fleet | None":
    """The account's state, for surfaces that render work orders an outage could hold.

    ONE READ FOR A WHOLE LISTING, and only when it can change a word: `fleet.current`
    opens every project's store, which is not a cost to pay per row, nor on a page where
    nothing is in `FLEET_HELD_STATUSES`. None when the catalog cannot be resolved — a
    surface that cannot answer "is the account up" still has to render (issue #714).
    """
    if not any(wo["status"] in invariants.FLEET_HELD_STATUSES for wo in wos):
        return None
    try:
        return fleet.current(resolve_catalog())
    except (OpsError, CatalogError):
        return None


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


#: The environment variable `dispatch._write_worker_settings` stamps on every dispatched
#: worker session, inherited by every subagent and every shell it opens. Its ABSENCE is
#: what `user_authorship` treats as proof the caller is not a worker.
WORKER_SESSION_ENV = "JARVIS_WO_ID"


def user_authorship(relay: bool) -> str:
    """`MESSAGE_AUTHOR_USER` if this call can be attributed to the human, else `''`.

    Two conditions, and they are load-bearing in different ways.

    `relay` says the CALL SITE is a surface a human reaches — `jarvis wo send`, the
    dashboard's message box, `jarvis neo answer`. It is a property of the code, not of
    any argument the caller supplies, so grepping for it enumerates the trusted surfaces
    exactly. That is deliberate: `--source` used to be the closest thing to attribution
    and a worker could pass any value it liked.

    The environment check is the enforcement. A worker reaches those same surfaces — it
    has a shell and `jarvis` on PATH — so `relay` alone would let it write its own
    authorisation into its own conversation and have the gate panel believe it. A
    dispatched session cannot shed `JARVIS_WO_ID` by passing a different flag.

    WHAT THIS DOES NOT CLAIM, because the boundary does not exist to claim it: worker and
    human run as the same uid, so nothing here survives a worker that deliberately
    scrubs its own environment, and no secret the CLI can read is one the worker cannot.
    The property held is narrower and is the one the incident needed — no sanctioned path
    mints a user stamp for a worker. §3 of
    docs/superpowers/specs/2026-09-11-a-gate-request-carries-the-users-words.md.
    """
    from .project_store import MESSAGE_AUTHOR_USER

    if not relay or os.environ.get(WORKER_SESSION_ENV, "").strip():
        return ""
    return MESSAGE_AUTHOR_USER


def send_message(wo_id: str, content: str, source: str = "jarvis",
                 project_name: str | None = None, relay: bool = False) -> dict[str, Any]:
    name, path, wo = find_work_order(wo_id, project_name)
    if wo["status"] in ("completed", "failed", "cancelled"):
        # Still allowed — resuming a finished session is fine — but tell the user.
        note = f"note: work order is {wo['status']}; the session will be revived"
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
    # Recorded, unargued, in front of NOBODY — `gates.AWAITING_CASE`. Without this branch
    # the park falls all the way to the catch-all and is reported as a permission prompt,
    # which is the false diagnosis of GitHub issue 100 arriving down the road
    # `invariants._waiting_on_neo_gate` was written to close. Wording mirrors what
    # `jarvis gate list` prints, the one surface that already gets it right.
    held = store.held_approvals(wo_id)
    if held:
        # Both exits, addressed by REQUEST NUMBER: this describes what the worker must
        # type, and a worktree-isolated worker cannot pass its own command string back
        # (spec 2026-09-12 §8). And the TTL abandons rather than refuses — §4.
        return {"what": "gate_held", "stalled": False,
                "detail": f"gate {held[0]['id']} is recorded but unargued — no reviewer "
                          f"sees it yet, and the move is the WORKER's, either "
                          f"`jarvis gate request {held[0]['id']} --why \"…\" "
                          f"--evidence \"…\"` or, if the gate matched it by mistake, "
                          f"`jarvis gate contest {held[0]['id']} --why \"…\"`. If neither "
                          f"comes the OS abandons it unreviewed on the "
                          f"`gates.case_ttl_seconds` timer"}
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
        # ...unless the delivery this promises has stopped happening. Answering
        # `queued_message` for a message the worker will never see is the sentence
        # GitHub issue 43 measured being wrong for 62 minutes, and MESSAGE_STUCK_BLOCKER
        # sends the user to this command to find out why — so it has to be able to say.
        # `stalled` stays False deliberately: a nudge is another message into the same
        # stalled queue, which is the one thing that cannot help.
        from .invariants import stuck_message

        found = stuck_message(store, wo)
        if found is not None:
            msg, why = found
            return {"what": "message_stuck", "stalled": False,
                    "detail": f"message {msg['id']} is queued undelivered because "
                              f"{why} — read `jarvis wo show {wo_id}`, then `jarvis wo "
                              f"done` if the work order is finished with it"}
        return {"what": "queued_message", "stalled": False,
                "detail": f"{len(queued)} message(s) queued — jarvisd delivers them "
                          f"when the worker is idle"}
    # Parked on the user's Claude Code sign-in (`Daemon._park_on_signin`). It reaches
    # this function as a `waiting_input` order with no session running and nothing
    # queued, which is EXACTLY the shape the fall-through calls a permission prompt — so
    # without this the one command that exists to say what a work order is waiting on
    # would name the wrong thing, and name it confidently.
    pause = worker_session.turn_pause(store, wo_id)
    if pause is not None and pause.reason == worker_session.PAUSE_AUTH:
        return {"what": "signin", "stalled": False,
                "detail": "Claude Code could not authenticate — run `/login`, and the "
                          "OS resumes this and every other parked order by itself"}
    # ...and the two pauses that DO name a moment. `Daemon.retry_paused_turns` relaunches
    # these unaided, so the work order is waiting on the OS. Without this branch the
    # fall-through calls a scheduled retry an unanswered permission prompt — and
    # `invariants.parked_reason`, which reads this answer to decide whether anything is
    # coming, would inherit the mistake as an attention item for a turn already booked in.
    if pause is not None and pause.resumable:
        return {"what": "retry_pending", "stalled": False,
                "detail": f"the turn stopped on a "
                          f"{worker_session.PAUSE_NOUN[pause.reason]} error and the OS "
                          f"relaunches it by itself"}
    # A live turn means "working" for every status EXCEPT `waiting_input`, where it means
    # the opposite: a permission prompt blocks INSIDE the turn, so the process is alive
    # and going nowhere. That is the one case this command was written for, and reading
    # the turn row the other way round would make it refuse the only thing it can fix.
    turn = store.latest_turn(wo_id)
    if (turn is not None and turn["state"] == "running"
            and wo["status"] != "waiting_input"):
        return {"what": "turn_running", "stalled": False,
                "detail": "a turn is in flight — the worker is working"}
    # Before the statuses below, because since issue 212 a round runs in PARALLEL with a
    # `needs_review` park — and "nothing is running to nudge" is exactly the false
    # diagnosis this command exists to stop giving.
    if round_note:
        return {"what": "validating", "stalled": False,
                "detail": f"the validation panel is judging it — the verdict settles "
                          f"the work order by itself{round_note}"}
    # ABOVE both catch-alls below, which answer "not dispatched yet — no worker exists to
    # nudge" and "nothing is running to nudge": true, useless, and confidently wrong about
    # the way through. A nudge cannot move this; the user's ruling on the PLANNER can, so
    # `stalled` stays False (spec 2026-09-24-a-planner-assumption-holds-its-feature §2.3).
    hold = store.plan_hold(wo)
    if hold:
        return {"what": "plan_assumptions", "stalled": False,
                "detail": f"its feature's plan is waiting on you — {hold['n']} "
                          f"assumption(s) on {hold['planner_id']}; "
                          f"`jarvis wo review {hold['planner_id']}`"}
    if wo["status"] in ("completed", "cancelled", "failed", "waiting_pr_merge",
                        "needs_review"):
        return {"what": wo["status"], "stalled": False,
                "detail": f"the work order is {wo['status']} — nothing is running to "
                          f"nudge"}
    if wo["status"] == "pending":
        return {"what": "pending", "stalled": False,
                "detail": "not dispatched yet — no worker exists to nudge"}
    # THE FALL-THROUGH BELOW USED TO ANSWER FOR THIS ONE, and it was the OS giving a
    # confident wrong explanation for a state it had created itself: a manager parked
    # between its feature's messages reached here as `waiting_input` with nothing
    # running and nothing queued, which is exactly the shape the last line calls a
    # permission prompt — impossible under `auto` (GitHub issue #264).
    if wo["status"] == "idle":
        return {"what": "manager_idle", "stalled": False,
                "detail": "it is the feature's manager and has nothing to act on — "
                          "idle until its feature sends it something, which is what "
                          "this work order is for"}
    return {"what": "prompt", "stalled": True,
            "detail": "nothing else accounts for it: an unanswered permission prompt "
                      "is what is left"}


#: The `waiting_on` answers where a nudge is ACTIVELY WRONG rather than merely useless,
#: mapped to the sentence that says why. They need their own refusal because every other
#: one below is bought by `could_prompt` being False — so a project running a mode that
#: CAN prompt would fall through and send the message anyway.
#:
#: `message_stuck`: the nudge is `send_message`, another row on the queue that is already
#: not moving, and `invariants.MESSAGE_STUCK_BLOCKER` sends the user to this command.
#:
#: `manager_idle`: the nudge buys a turn whose whole content is the worker saying nothing
#: was needed, and then the OS parks it back where it was — the loop GitHub issue #264
#: measured at 0.37 USD a lap.
NUDGE_IS_WRONG = {
    "message_stuck": "a nudge cannot help — it is another message on the queue that is "
                     "already stuck.",
    "manager_idle": "a nudge cannot help — it buys one turn of the manager saying "
                    "nothing was needed, and it ends up back here.",
}


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
    """Record an assumption: DB row + ASSUMPTIONS.md append + review flag.

    THE FLAG IS NOT UNCONDITIONAL. `invariants.neo_reviews_later` is the same predicate
    `true_blockers` derives with, asked here so the write and the derivation cannot
    disagree for the seconds before the next reconcile tick (issue #711).
    """
    from .invariants import neo_reviews_later

    name, path, wo = find_work_order(wo_id)
    store = ProjectStore(path)
    try:
        store.add_assumption(wo_id, content)
        if not neo_reviews_later(store, wo):
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
    """One validation round on one line: number, fingerprint, outcome, reason.

    A round is NOT deliberation. The number, the outcome and the reason are what the
    submitter was told, so they belong on every surface a person reads by default; what
    stays behind `jarvis validation show` is the seats — their verdicts and their raw
    replies. One formatter, so the CLI's two `show` commands cannot word the same round
    two different ways.

    The config version is the round's OTHER input — what judged it, beside the
    `fingerprint` of what was judged — and `not recorded` is the only honest reading of a
    NULL stamp (config-console design §5).
    """
    reason = (rnd.get("reason") or "").strip()
    # The COMMIT is the round's third input, beside what was judged and what judged it,
    # and it is the one an automatic merge is bound to — so a reader asking why a pull
    # request did not merge itself can see the answer on the round rather than having to
    # infer it (spec 2026-09-14 §5.2). `not recorded` for a worktree packet and for every
    # round written before the column existed, which is the honest reading of both.
    sha = str(rnd.get("head_sha") or "")
    # WHO OPENED IT, and only when the answer is not "a submission did". A round a person
    # forced must never read afterwards like a worker re-delivering — that
    # indistinguishability is the whole defect `jarvis validation force` removes — and the
    # place it has to say so is the line every surface already prints.
    forced = (rnd.get("forced_reason") or "").strip()
    # WHAT THE ROUND FILED RATHER THAN BLOCKED ON. A count only: the items themselves are
    # deliberation and live behind `jarvis validation show`, and naming the seat that
    # raised one here would put a seat's name on a surface the submitter reads. Silent at
    # zero — a line saying "0 follow-ups" on every round in the fleet would spend
    # attention on an absence, the way `automerge_state` returns None rather than "off".
    filed = rnd.get("follow_ups") or NO_FOLLOW_UPS
    n_filed, dropped = len(filed.get("filed") or ()), int(filed.get("dropped") or 0)
    failed, n_kept = int(filed.get("failed") or 0), len(filed.get("withheld") or ())
    # Assembled as a LIST and joined, not appended to a string: the three are
    # independent, any subset can be empty, and string-appending a separator per clause
    # is how a round that only FAILED came out as "· , · 6 not filed".
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
    # THE WORD, not the raw outcome: `failed` is three different facts and only
    # `validation_standing` knows which one this row is (GitHub issue #581).
    word, _tone, _icon = validation_standing(rnd)
    return (f"round {rnd['round']} · {rnd['fingerprint']} · {word}"
            f" · config {rnd.get('config_version') or 'not recorded'}"
            f" · commit {sha[:10] or 'not recorded'}"
            + note
            + (f" · forced: {forced}" if forced else "")
            + (f" — {reason}" if reason else ""))


#: How each `wo_alarms.status` reads to a person, frozen with the statuses themselves in
#: §4 of docs/superpowers/specs/2026-08-31-the-supervisor.md. `raised` is the COMMON case,
#: not the interesting one: the supervisor ships off.
ALARM_STANDING = {
    "raised": "raised",
    "reviewing": "with the supervisor",
    "acked": "acked by the supervisor",
    "escalated": "escalated to Neo",
    "proposed": "a remedy proposed",
    "skipped": "not reviewed",
    "failed": "supervisor failed",
}


def turn_label(seq: int | None) -> str:
    """"turn 3", or "no turn" for a finding that judged a subject rather than a turn.

    One formatter for the same reason `alarm_standing_line` is one: `wo_alarms.seq` is
    NOT NULL and a subject-level finding stores `project_store.NO_TURN`, so every
    surface that prints it would otherwise be one edit away from showing the user
    `turn -1`. §1 of docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md.
    """
    return "no turn" if seq is None or seq == NO_TURN else f"turn {seq}"


def alarm_kind_label(alarm: dict[str, Any],
                     titles: dict[str, dict[str, str]]) -> str:
    """WHAT RAISED THIS ALARM, as user copy: a probe's title, or a cost alarm's kind.

    `wo_alarms.kind` holds the probe id for a health finding, and a kebab id is a
    database value — `no-progress` on the page where `Nothing is moving` belongs (§6).
    A cost alarm keeps the rendering it has had since PR 159, so this is additive on
    every row that existed before the sweep.

    `titles` is `probe_titles()`, passed in rather than resolved here: this is called
    once per row and resolving it inside would re-read the catalog for each one. Falls
    back to the id for a probe the catalog no longer has, because a finding outlives the
    setting that raised it and an empty cell says less than the id does.
    """
    if alarm.get("source") == "health" and alarm.get("probe"):
        by_project = titles.get(str(alarm.get("project") or ""), {})
        return by_project.get(str(alarm["probe"])) or str(alarm["probe"])
    return str(alarm.get("kind") or "")


def remedy_line(alarm: dict[str, Any]) -> str:
    """One alarm's remedy on one line, or "" when none was proposed.

    THE TERMINAL'S HALF of `_alarm.html`'s `remedy_block`: the same three facts in the
    same order — what was asked for, who is holding the decision, and whether anything
    has actually happened — because a reader moving between the page and the CLI being
    told two different things is the failure `alarm_standing_line` also exists to stop.
    §6 of docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md.
    """
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
    """One work order's alarms on one line: how many, how they stand, and their ids.

    `round_line`'s job for the other thing that judges a work order — one formatter, so
    the surfaces cannot word the same standing two different ways. Pure: it reads the
    `wo_alarms` rows the caller already has and opens nothing.

    The ids are the point of the line. An alarm is an object with a page of its own now
    (`/alarms/<project>/<al-id>`, `jarvis alarms show`), so a count with no ids tells a
    reader something is there and gives them no way to reach it.
    """
    if not alarms:
        return ""
    counts = Counter(a["status"] for a in alarms)
    order = [*ALARM_STANDING, *sorted(k for k in counts if k not in ALARM_STANDING)]
    standing = ", ".join(f"{counts[s]} {ALARM_STANDING.get(s, s)}"
                         for s in order if counts.get(s))
    return f"{len(alarms)} ({standing}) — " + ", ".join(a["id"] for a in alarms)


#: The event one filing writes, and the only link between a round and the issues it
#: produced. Spec §4.6:
#: docs/superpowers/specs/2026-09-15-the-panel-blocks-on-blockers.md
#:
#: ITS PAYLOAD DEPARTS FROM §4.6's `{round, round_id, ids, seats, dropped}` because the
#: destination changed (user ruling, 2026-09-16: GitHub issues, not backlog rows). `items`
#: carries the url, number, title and seat of each issue TOGETHER rather than as parallel
#: lists, and `failed` is new — filing can now fail on a network, which a local insert
#: could not.
FOLLOW_UPS_EVENT = "validation_follow_ups_filed"

#: What a round with nothing filed projects. PRESENT ON EVERY ROUND, which is the rule
#: `validation_rounds`, `assumptions` and `alarms` already follow: a key that comes and
#: goes is a key every consumer has to guard, and a Jinja template guards it by
#: rendering nothing at all (`kn-99e37a4b`).
NO_FOLLOW_UPS: dict[str, Any] = {"filed": [], "withheld": [], "dropped": 0,
                                 "failed": 0}


def follow_up_key(title: Any) -> str:
    """The dedupe key for one finding's title: stripped, whitespace collapsed.

    The house normalisation, the one `evidence._normalise` applies for the same reason.
    NOT hashed, though §4.4 calls it a digest: the comparison is a set lookup in memory,
    a hash buys nothing there, and the key is worth being able to read in a log line.
    """
    return " ".join(str(title or "").split())


#: The token that identifies one finding in BOTH published title shapes, and therefore
#: the thing the dedupe survives a privacy flip on. `vf` for validation follow-up.
#: Anchored to eight hex characters so a person's own issue cannot accidentally carry one.
FOLLOW_UP_TOKEN_RE = re.compile(r"\bvf-[0-9a-f]{8}\b")

#: What a withheld follow-up USED TO BE CALLED on a tracker the OS could not establish is
#: private. No issue is opened for one any more (see `file_validation_follow_ups`); this
#: names the rows filed before that, which is what `jarvis issues` needs it for.
WITHHELD_TITLE = "Validation follow-up"


def follow_up_digest(title: Any) -> str:
    """The stable per-finding token, derived from `follow_up_key(title)` and nothing else.

    That derivation is the whole trap-avoidance: the SAME finding gets the same token in
    the full shape and in the withheld one, so a unit whose round 1 filed against a
    private repository and whose round 2 reads public still dedupes (spec §9).

    sha256 rather than `hash()`, which is salted per process and would make the token a
    different string on every daemon restart.
    """
    return "vf-" + hashlib.sha256(follow_up_key(title).encode()).hexdigest()[:8]


def follow_up_token(title: Any) -> str:
    """The token carried by a title already on the tracker, or "" if it carries none.

    "" IS THE PRE-CHANGE ISSUE — every follow-up filed before spec §9 landed is a bare
    finding title — and the caller falls back to `follow_up_key` for exactly those.
    """
    m = FOLLOW_UP_TOKEN_RE.search(str(title or ""))
    return m.group(0) if m else ""


def follow_up_claim(finding: Mapping[str, Any]) -> str:
    """THE DEDUPE KEY: the code a finding names, plus what it claims about it.

    `sha256(title)` was the key until wo-3619e6e4, and a seat writes a slightly different
    sentence for the same nit every round — so the same observation was filed three times
    on wo-0a9ba9b3 (#538, #546, #552). `file` and `symbol` are the seat's own anchors and
    do not move when the sentence is reworded; the title rides along because one symbol
    can carry two different claims.

    Falls back to the title alone for a finding that names no code — those are
    inadmissible now, but `follow_up_digest` is also computed over pre-schema rows.
    """
    parts = [str(finding.get("file") or "").strip().lower(),
             str(finding.get("symbol") or "").strip(),
             follow_up_key(finding.get("title"))]
    return "|".join(p for p in parts if p)


def follow_up_digest_of(finding: Mapping[str, Any]) -> str:
    """One finding's token, over `follow_up_claim` rather than over its title alone."""
    return "vf-" + hashlib.sha256(follow_up_claim(finding).encode()).hexdigest()[:8]


#: What a follow-up must be about before it may become a tracker issue: a behaviour that
#: is WRONG, named against the code it is wrong in. "No test covers this", "the docstring
#: is stale", "the PR body does not say", "CI has not run" are legitimate things to tell
#: the SUBMITTER — `follow_up_feedback` routes them there — and are an unbounded supply
#: in any codebase, which is what filled a public tracker with 40 issues.
def follow_up_admissible(finding: Mapping[str, Any]) -> bool:
    """Does this finding name a wrong behaviour, in a named file, with a failure?

    STRUCTURE, NOT PROSE. The seat mandates ask for `file` and `failure` on every
    follow-up (spec §4.4); a finding that cannot fill them in is one nobody could act on
    from the tracker either. Classifying the title text instead would be a blocklist over
    model prose — it catches the phrasings somebody thought of.
    """
    return bool(str(finding.get("file") or "").strip()
                and str(finding.get("failure") or "").strip())


def follow_up_feedback(follow_ups: Sequence[Mapping[str, Any]]) -> str:
    """The inadmissible findings, as a note for the SUBMITTER, or "".

    They are not dropped: in the round the submitter is already being sent back, a stale
    docstring or a missing test costs nothing and is fixed in the same session. This is
    where they go instead of the tracker.
    """
    lines = [f"- {follow_up_key(f.get('title'))}"
             for f in follow_ups if not follow_up_admissible(f)
             and follow_up_key(f.get("title"))]
    if not lines:
        return ""
    return ("\n\nAlso raised, and not blocking — worth fixing here rather than "
            "carrying:\n" + "\n".join(lines))


def follow_up_title(key: str, digest: str) -> str:
    """What the issue is called on the tracker: the finding's own title and its token.

    ONE SHAPE SINCE wo-3619e6e4. The withheld shape — `WITHHELD_TITLE` and a digest —
    was what a follow-up got on a tracker the OS could not establish is private, and it
    named nothing a reader could act on; that finding is now kept internally and no issue
    is opened, so the only issue this writes is one whose text may travel (spec §9).
    """
    return f"{key} [{digest}]"


def follow_up_repo(project_path: Path) -> str:
    """`owner/name` of the repository a follow-up for this project belongs on, or "".

    Read from the CHECKOUT's own `origin` and from nowhere else. That is the point: the
    repository name becomes a `gh --repo` argument, and the one source that no model and
    no stored string can influence is the local remote (`github.LOCAL_GIT_READS`).

    "" for a project with no `origin` at all — several in the fleet are local-only — and
    the caller reports that as a filing it could not make rather than as a finding that
    did not exist.
    """
    from . import github

    pair = github.origin_repo(project_path)
    return f"{pair[0]}/{pair[1]}" if pair else ""


def file_validation_follow_ups(store: ProjectStore, project: ProjectSpec,
                               round_row: Mapping[str, Any],
                               follow_ups: Sequence[Mapping[str, Any]],
                               cfg: Any, *, wo_id: str | None = None,
                               fo_id: str | None = None) -> dict[str, Any]:
    """Keep this unit's non-blocking findings — on the tracker if they may travel there.

    Called by both daemon validation loops when the loop SETTLES, from the last judged
    round only, and never from an intermediate one (spec §4.4). Round 1's findings used
    to be filed at once and then fixed in round 2, leaving issues open against code that
    no longer exists; an issue now always describes the code that shipped.

    **A FINDING THE TRACKER CANNOT CARRY DOES NOT BECOME AN ISSUE.** The privacy read
    that decides whether a seat's words may be published (spec §9) decides whether an
    issue is opened at all: what a withheld one produced was a row titled `Validation
    follow-up vf-…` over boilerplate saying the text is elsewhere — twenty of them on
    wo-0a9ba9b3 — which is backlog with no information in it. Withheld findings are kept
    WHOLE in `internal_follow_ups` instead, and `jarvis issues` and the project page mark
    them internal-only. Nothing changes on a repository the OS establishes IS private.

    A project with no `origin` takes the same path: no repository is no proof of privacy,
    and the finding is kept rather than counted as a failure the way it was before.

    **THE CAP IS PER ORDER.** `cfg.max_follow_ups` bounded one ROUND, so a unit judged
    four times could file four times the cap. What the order has already raised — tracker
    links plus internal rows, both local reads — is subtracted first.

    **NOTHING INADMISSIBLE IS FILED.** `follow_up_admissible` is the gate; the rest go to
    the submitter through `follow_up_feedback`, in the round, where they cost nothing.

    **NOT IN `validation.py`.** That module's contract is "it is called; it is never
    messaged, and it messages nobody" — a panel that files GitHub issues has a side
    effect the round machine cannot roll back when the round later fails on transport.

    **NOT IN `daemon.py` either.** One function, two callers, for the reason
    `side_effects_of` and `collect_feature_evidence` are already here.

    **THE SEAT'S WORDS GO OUT ONLY TO A TRACKER SHOWN TO BE PRIVATE** (spec §9).

    **A FAILURE IS COUNTED, NEVER SWALLOWED.** Filing crosses a network, so it can fail:
    no `gh`, no credentials, a rate limit. Each such finding lands in `failed`, because a
    finding that silently evaporated is indistinguishable from a round that raised none.

    Returns the event payload whether or not anything was written.
    """
    from . import issues

    n, round_id = int(round_row["round"]), int(round_row["id"])
    unit_id = wo_id or fo_id or ""
    payload: dict[str, Any] = {"round": n, "round_id": round_id, "unit": unit_id,
                               "items": [], "withheld": [], "dropped": 0, "failed": 0}
    # `follow_ups` is read defensively by the CALLER too (`verdict.get(...) or ()`):
    # `Daemon.validator` is injectable and several suites inject fakes that return only
    # the three keys that predate this.
    if not getattr(cfg, "follow_ups", True) or not follow_ups:
        return payload

    # ADMISSIBLE, DISTINCT, and no network yet: two seats can reach the same nit in one
    # round, and the tracker cannot dedupe an issue this round has not filed yet.
    pending = _distinct([f for f in follow_ups if follow_up_admissible(f)])
    if not pending:
        return payload

    kept = {str(row["digest"]) for row in store.internal_follow_ups(unit_id)}
    # THE ORDER'S OWN HISTORY, both halves, both local. `issue_links_of` is the relation
    # `jarvis wo show` reads; counting the tracker over the network instead would make
    # the cap depend on a call that is allowed to fail.
    raised = len(kept) + len([r for r in store.issue_links_of(unit_id)
                              if r["kind"] == "raised"])
    cap = int(getattr(cfg, "max_follow_ups", DEFAULT_VALIDATION_FOLLOW_UP_CAP))
    room = max(0, cap - raised)

    repo = follow_up_repo(project.path)
    # ONE privacy read, and it fails closed: anything but a positive "this repository is
    # private" withholds the finding — and now withholds the issue with it.
    private = bool(repo) and issues.repo_is_private(repo)
    if not private:
        # AND NOT ONE THE TRACKER ALREADY CARRIES. A repository flipped open between two
        # settles would otherwise record a second, internal copy of a finding that is
        # already an issue — the same trap the digest was built for (`kn-4fbf00ee`), one
        # destination along. Read off the filing events, so it costs no network.
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
        # ONE round trip for the dedupe, whatever the cap — see `issues.follow_ups_filed`
        # for why it must read `--state all`.
        #
        # THREE KEYS PER FILED ISSUE, because the digest has been derived two ways. The
        # claim digest is the live one; `follow_up_digest(title)` matches everything
        # filed between spec §9 and wo-3619e6e4; the plain title matches everything older
        # than §9, which carries no token at all.
        already, already_keys = set(), set()
        for row in issues.follow_ups_filed(repo, unit_id):
            already.add(follow_up_token(row.get("title")))
            already_keys.add(follow_up_key(row.get("title")))
        already.discard("")
    except GitHubError as e:
        # The tracker could not be READ, so nothing may be written: filing blind would
        # duplicate every follow-up this unit already has.
        log.info("[%s] %s: could not read %s for dedupe: %s",
                 project.name, unit_id, repo, e)
        payload["failed"] += len(pending)
        payload["reason"] = "the tracker could not be read"
        return _record_filing(store, payload, wo_id=wo_id, fo_id=fo_id)

    # DEDUPE BEFORE THE CAP, AND THE ORDER IS THE WHOLE CORRECTNESS OF THIS.
    #
    # Capping first reads as the tidier rule — bound the work before doing any of it —
    # and it STARVES: a unit whose first `cap` findings are already filed spends the
    # whole cap on duplicates and never reaches the ones behind them. Caught by a test,
    # not by review.
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
        # `title` ON THE EVENT IS THE FINDING'S OWN, never what the tracker was told. The
        # record is internal and the surfaces that print a filed issue beside the finding
        # that caused it key on exactly this (`cli._finding_lines`).
        payload["items"].append({"url": url, "number": issues.issue_number(url),
                                 "title": key, "seat": seat, "withheld": False})
        # THE RELATION, beside the event. The event says what this ROUND did; this says
        # what the ORDER has raised. `announced` because `_follow_up_body` has just
        # written that sentence into the issue itself.
        store.record_issue(url, number=issues.issue_number(url), repo=repo, title=title,
                           state="OPEN")
        store.link_issue(url, unit_id, "raised", round_no=n, seat=seat, announced=True)
    return _record_filing(store, payload, wo_id=wo_id, fo_id=fo_id)


def _distinct(follow_ups: Sequence[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """The findings worth trying to file: titled, and one per normalised title.

    A finding with nothing to call it cannot be an issue anybody can act on, and it is
    dropped SILENTLY rather than counted — `dropped` means "over the cap, a later round
    may raise it again", and a titleless finding retried later produces the same nothing.
    """
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
    """Write the event, unless the round had nothing at all to say."""
    if (payload["items"] or payload["withheld"] or payload["dropped"]
            or payload["failed"]):
        if wo_id:
            store.add_event(wo_id, FOLLOW_UPS_EVENT, payload)
        else:
            feature_event(store, str(fo_id), FOLLOW_UPS_EVENT,
                          {**payload, "feature_order": fo_id})
    return payload


def _follow_up_body(finding: Mapping[str, Any], *, unit_id: str, round_no: int,
                    pr_url: str, seat: str) -> str:
    """The issue body ON A PRIVATE TRACKER: the finding's own detail, then where it came
    from. `_withheld_body` above is what a public or unestablished one gets (spec §9).

    THE SEAT IS NAMED HERE, and this is one of the two places it may be (the other is
    `jarvis validation show`). Deliberation never reaches the submitter, but the tracker
    is the project's own record, read later by a person deciding whether to act, and
    which reviewer said this is exactly what they need. This door is deliberate; do not
    close it (spec §4.2).

    The footer is the only context whoever picks this up will have: the seat's sentence
    alone does not say which change it was written about.
    """
    detail = str(finding.get("detail") or "").strip()
    # WHAT MAKES THE ISSUE ACTIONABLE, and what `follow_up_admissible` required before
    # this could be filed at all: the code it is about and the way it goes wrong.
    where = " ".join(x for x in (str(finding.get("file") or "").strip(),
                                 str(finding.get("symbol") or "").strip()) if x)
    failure = str(finding.get("failure") or "").strip()
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
                      *footer])


def filed_follow_ups(store: ProjectStore, *, wo_id: str | None = None,
                     fo_id: str | None = None) -> dict[int, dict[str, Any]]:
    """What the panel filed, per round id, for the surfaces that print a round.

    ONE resolver, because `validation_rounds` and `validation_detail` are two different
    projections read by four surfaces between them, and two of them computing this
    separately is how they come to show different things (`kn-4ea33fe6`, `kn-99e37a4b`).

    Read from the EVENT alone — no network, ever. A surface that asked GitHub what a
    round filed would make `jarvis wo show` fail when the tracker is unreachable, and
    would re-read one issue per round per page view. The event is what the OS recorded
    that it did, which is the honest thing for a record to show.

    Free on a fleet that has never filed one: no event, no work.
    """
    rows = (store.events_of_kind(wo_id, FOLLOW_UPS_EVENT) if wo_id
            else feature_events_of_kind(store, str(fo_id), FOLLOW_UPS_EVENT))
    if not rows:
        return {}
    out: dict[int, dict[str, Any]] = {}
    for row in rows:
        p = db.from_json(row["payload"], {})
        # A round RETRIED after a transport failure files again and writes a second
        # event. The tracker deduped the writes, so the two events are a partition of one
        # round's filing: items accumulate, and `dropped`/`failed` are the LAST attempt's
        # — the attempt that decided what is still missing.
        rnd = out.setdefault(int(p.get("round_id") or 0),
                             {"filed": [], "withheld": [], "dropped": 0, "failed": 0})
        for item in p.get("items") or ():
            if isinstance(item, Mapping) and item.get("url"):
                rnd["filed"].append(dict(item))
        # KEPT, NOT FILED. No URL and nothing to link, so it cannot ride in `filed` —
        # and a round whose findings all went this way would otherwise read as a round
        # that raised nothing at all.
        for item in p.get("withheld") or ():
            if isinstance(item, Mapping) and item.get("title"):
                rnd["withheld"].append(dict(item))
        rnd["dropped"] = int(p.get("dropped") or 0)
        rnd["failed"] = int(p.get("failed") or 0)
        if p.get("reason"):
            rnd["reason"] = str(p["reason"])
    return out


# -- the issue index: which orders point at which issues, and how many ---------------
#
# The relation `filed_follow_ups` could not express. That resolver answers "what did THIS
# ROUND file", which is a fact about one round; these answer "what has this ORDER raised"
# and "how many orders have pointed at this issue" — the second of which did not exist at
# all, and is the priority signal the user asked for. Local, both directions, no network.

#: A `#N` in a brief. Anchored against a preceding word character or slash so a URL's own
#: fragment and an `abc#1` cannot match — this is the GitHub shorthand, written on purpose.
ISSUE_MENTION_RE = re.compile(r"(?<![\w/])#([0-9]{1,7})(?![0-9])")

#: SOMETHING ISSUE-URL-SHAPED in a body of prose. `issues.ISSUE_URL_RE` is anchored at
#: both ends — it answers "is this string a URL", which is the question a write asks —
#: and cannot scan. This one only FINDS candidates: the host and the repository are left
#: out of the capture on purpose, because nothing here decides whether a match belongs to
#: the project. `issue_url_for` decides that, by string equality, for both shapes below.
ISSUE_LINK_RE = re.compile(
    r"https://[A-Za-z0-9.-]+/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+/issues/([0-9]+)(?![0-9])")


def issue_url_for(repo: str, number: int) -> str:
    """THE ONE DEFINITION of what a project's own issue URL looks like — host included.

    Every path that decides "is this issue ours" compares against this rather than
    picking fields out of a URL, because a comparison that reads only the owner and the
    repository accepts `https://attacker.example/<owner>/<repo>/issues/7` as the
    project's own. That URL then reaches `tracked_issues` under the real repository name
    and the daemon's sweep points `gh` at a host nobody in this project chose. A brief is
    not always written by the user (the tracker is public), so the destination of a write
    must never be derived from prose — only ever compared with a locally-derived one.
    """
    return f"https://github.com/{repo}/issues/{number}"


def is_own_issue_url(url: str, repo: str, number: int) -> bool:
    """Case-insensitive because GitHub is; host, owner, repository and number all count."""
    return (url or "").lower() == issue_url_for(repo, number).lower()


def issue_citations(text: str, repo: str, known: Collection[str]) -> list[str]:
    """Every issue on `repo` that `text` deliberately cites. Conservative by design.

    TWO SHAPES, AND THE SECOND IS DELIBERATELY NARROWER THAN THE FIRST.

    * A FULL ISSUE URL on this project's own repository counts outright: `/issues/N`
      cannot be anything but an issue, and nobody pastes one by accident. ON THIS
      PROJECT'S OWN is decided by `is_own_issue_url` — the whole URL, host and all — and
      what is stored is the URL that check was made against, never the prose's own text.
    * A BARE `#N` counts only for an issue the project ALREADY TRACKS. `#N` is also how
      a pull request is written, and the OS cannot tell the two apart without a network
      call it must not make here — so an unrecognised number is left alone rather than
      becoming a link to an issue that may not exist. Every issue whose count the user
      reads is already tracked (the panel filed it, or an order was dispatched at it),
      so the narrowing costs the signal nothing.

    A passing mention in a result summary is not a citation and never reaches here: the
    only text this is asked about is the brief the work order was created with.
    """
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
    """Link a freshly created work order to the issues its brief points at.

    ONLY ISSUES ON THE PROJECT'S OWN `origin`, which is one rule doing two jobs: it is
    the locally-derived repository name that `issues.checked_issue_url` will later hold
    every write to (`kn-2531869c`), and it is what keeps the count a fact about this
    project's tracker rather than a half-populated view of the whole fleet.

    Never raises: a work order must not fail to be created because a reference could not
    be recorded.
    """
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
    """Is `url` an issue URL on `repo`? The local half of `checked_issue_url`.

    The anchored match is what proves the string is a URL and nothing but a URL; the
    NUMBER is all that is taken from it, and the rest is decided against
    `issue_url_for` — see there for why no field of a URL may be trusted on its own.
    """
    from .issues import ISSUE_URL_RE

    match = ISSUE_URL_RE.match(url or "")
    return bool(match) and is_own_issue_url(url, repo, int(match.group(4)))


#: What a unit with no linked issue projects. ALWAYS PRESENT, `NO_FOLLOW_UPS`'s rule:
#: a key that comes and goes renders as silence in Jinja and as a `KeyError` nowhere.
NO_ISSUES: dict[str, Any] = {"raised": [], "references": [], "withheld": []}


def issue_index(store: ProjectStore, unit_id: str) -> dict[str, Any]:
    """Every issue one order raised, and every other issue it points at.

    THE CONSOLIDATED LIST, across every round — which is the thing the per-round
    fragments could not be. An order judged over four rounds filed its follow-ups in four
    places and had no total anywhere; wo-61173704 filed fifteen that way.

    `state` is what the sweep last read off GitHub, "" until it has looked once. Cached
    rather than fetched for `filed_follow_ups`'s reason: `jarvis wo show` must not fail
    because the tracker is unreachable.

    THE TITLE IS THE FINDING'S OWN WHEREVER THE OS STILL HAS IT. `tracked_issues.title`
    is a cache of what the issue is CALLED ON GITHUB, which on a public tracker is
    `Validation follow-up vf-…` and nothing else — a list of fifteen of those tells the
    reader nothing. The words the seat wrote are on the filing event, they never left the
    OS, and these surfaces are the user's own. Same rule `cli._finding_lines` already
    follows for a withheld finding, and the reason the event keeps the finding's title
    rather than the tracker's.
    """
    titles = _filed_titles(store, unit_id)
    # WITHHELD FOLLOW-UPS ARE NOT ISSUES AND ARE STILL THIS ORDER'S. Nothing was opened
    # on the tracker for them (spec §9 decides the filing, not just the text), so the
    # only place they can be read is here and on `jarvis issues` — losing them would
    # trade forty empty issues for forty findings nobody can see.
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
    """`{issue url: the finding's own title}` for everything this unit filed.

    Off the events, so it costs a query and no network. Which kind of unit the id names
    is read off the id, `validation_view`'s idiom.
    """
    kind = {"fo_id": unit_id} if is_feature_order_id(unit_id) else {"wo_id": unit_id}
    out: dict[str, str] = {}
    for payload in filed_follow_ups(store, **kind).values():  # type: ignore[arg-type]
        for item in payload.get("filed") or []:
            url, title = str(item.get("url") or ""), str(item.get("title") or "")
            if url and title:
                out[url] = title
    return out


def issue_board(store: ProjectStore, limit: int = 50) -> list[dict[str, Any]]:
    """This project's tracked issues, most-referenced first.

    The ranking the user reads when choosing what to work on next: `refs` counts DISTINCT
    work orders, so "three separate pieces of work ran into this" is a number rather than
    a thing to go and count on GitHub.

    OPEN AND UNKNOWN-STATE ISSUES ONLY. A closed issue's reference count is history, and
    a ranking that carries it is a ranking with nothing to act on at the top.
    """
    rows = [{**row, "internal": False} for row in store.issue_board()
            if (row.get("state") or "") != "CLOSED"]
    return (rows + internal_board(store))[:limit]


def internal_board(store: ProjectStore) -> list[dict[str, Any]]:
    """The withheld follow-ups, in the shape `issue_board` rows have.

    THE SAME LIST, MARKED. A finding kept off the tracker is still a thing the project
    might pick up, and a surface that showed only what GitHub has would hide every
    finding on a public repository — which is most of them. `refs` is 1 by construction:
    an internal row belongs to the one order that raised it, so nothing can point at it
    the way a tracker issue can.
    """
    return [{"issue_url": "", "number": 0, "repo": "", "title": row["title"],
             "state": "", "refs": 1, "internal": True, "digest": row["digest"],
             "units": [{"unit_id": row["unit_id"], "kind": "raised",
                        "round": row["round"] or 0, "seat": row["seat"] or ""}]}
            for row in store.internal_follow_ups()]


def issue_board_across(project_name: str | None = None,
                       limit: int = 50) -> list[dict[str, Any]]:
    """`issue_board` for one project or for the whole fleet, ranked across all of them.

    Each row carries its `project`, because an issue number alone does not identify an
    issue once more than one tracker is in play — and the fleet-wide read is the default
    for the same reason `jarvis alarms` is: the user is choosing what to work on next,
    not auditing one project.
    """
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
    """One unit's rounds, oldest first, WITHOUT the seats' opinions.

    The projection the default documents carry — `jarvis wo show`, `jarvis fo show` and
    both dashboard pages. `summary` and `evidence` are dropped along with the opinions:
    they are a copy of what the unit already says about itself, and a round listing is
    read to answer "how many times, and what came back", not to re-read the submission.
    """
    filed = filed_follow_ups(store, wo_id=wo_id, fo_id=fo_id)
    return [{**{k: r[k] for k in ("id", "round", "ts", "fingerprint", "outcome",
                                  "reason", "pr_url", "config_version", "head_sha",
                                  "forced_reason", "hold_cause")},
             # ALWAYS PRESENT, even empty — the rule this key list, `assumptions` and
             # `alarms` already follow. `jarvis wo show`, `jarvis fo show` and both
             # dashboard pages read THIS projection, so a key that came and went would
             # be one every consumer had to guard (spec §4.7).
             "follow_ups": filed.get(int(r["id"]), NO_FOLLOW_UPS)}
            for r in store.validation_rounds(wo_id=wo_id, fo_id=fo_id)]


#: Every event the automatic merge writes. The ORDER HERE MEANS NOTHING — `automerge_state`
#: picks by timestamp — and the list exists only so that adding an event kind is one edit
#: rather than one edit and a forgotten renderer.
AUTOMERGE_EVENTS = ("automerge_merged", "automerge_decided", "automerge_proposed",
                    "automerge_failed", "automerge_held")

#: The one event that is TERMINAL: nothing follows a merge, so it wins over anything with
#: a later timestamp. Everything else is a stage the work order can leave.
AUTOMERGE_TERMINAL = "automerge_merged"


def automerge_state(store: ProjectStore, wo: dict[str, Any]) -> dict[str, Any] | None:
    """What `jarvis wo show` and the dashboard say about the automatic merge, or None.

    None — and therefore no line at all — for every work order the mechanism has never
    touched, which is all of them on a project that has not opted in. A surface that said
    "auto-merge: off" on every work order in the fleet would spend the user's attention
    on the absence of a feature they did not ask for.

    **Rendered from what the POLL RECORDED, not re-derived.** The interesting line —
    "round 2 passed on a1b2c3d, the head is now e4f5a6b" — needs the live head, and only
    the tick that held the merge ever knew it. Re-deriving here would mean a `gh` call
    from a CLI command, and it would answer about a different moment than the one the
    record is describing.

    **THE NEWEST EVENT WINS, BY TIMESTAMP, with one exception.** Reading the kinds in a
    fixed order and taking the first with any rows is the obvious implementation and it
    is wrong, because `gates.apply_decision` writes `automerge_decided` on EVERY verdict:
    an approval would then outrank every later event for ever, and a pull request whose
    head moved after the approval — or whose merge failed three times — would go on
    saying "approved by neo" while nothing was ever going to merge. That is precisely the
    case this function exists to surface.

    The exception is `automerge_merged`: nothing follows a merge, so it wins over
    anything later. Nothing writes a later row today; it is asserted rather than assumed
    because the cost of being wrong is a completed work order claiming to be held.

    **AND A REFUSAL IS STICKY FOR THE COMMIT IT JUDGED** — spec 2026-09-24 fix 4a. One
    tick of GitHub answering `mergeable: UNKNOWN` wrote a hold stamped after a denial, and
    `Daemon._note_automerge_held` dedupes per (sha, code, reason) so no newer row ever
    overtook it back: the line said the merge was held on mergeability, for ever, over a
    reviewer's refusal. A verdict is a permanent fact about one commit and a hold a
    transient one, so the transient must not bury it — but only about the SAME commit: a
    hold on a different head means the head moved, which is a new submission the denial
    does not describe. An APPROVAL gains no stickiness at all, which is the case the
    paragraph above exists for.
    """
    from . import db
    from .automerge import decided_sha

    newest: dict[str, Any] | None = None
    terminal: dict[str, Any] | None = None
    decided: dict[str, Any] | None = None
    for kind in AUTOMERGE_EVENTS:
        rows = store.events_of_kind(wo["id"], kind)
        if not rows:
            continue
        # The payload is spread FIRST so that `kind` and `ts` are this function's own
        # answers and not whatever an event happened to carry under those names.
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
    return {**newest, "line": _automerge_line(newest)}


#: Every event the automatic assumption review writes. `AUTOMERGE_EVENTS`' note applies
#: verbatim: the order means nothing, and the list exists so that adding a kind is one
#: edit rather than one edit and a forgotten renderer.
#:
#: The last five are the early pass's (docs/superpowers/specs/2026-09-23-an-assumption-
#: judged-while-the-worker-still-runs.md §4.4). They are enumerated here, once, so that
#: no section of that feature invents its own kind: a kind with no entry here is invisible
#: to `autoreview_state` and `assumptions_with_rulings`, and a kind with no branch in
#: `timeline` renders as generic "signal" prose that tells the reader nothing
#: (kn-3f133363).
AUTOREVIEW_EVENTS = ("autoreview_accepted", "autoreview_escalated", "autoreview_asked",
                     "autoreview_held", "autoreview_provisional", "autoreview_objected",
                     "autoreview_objection_withdrawn", "autoreview_confirmed",
                     "autoreview_unconfirmed")

#: The kinds whose prose asserts the USER OWES A DECISION, and the only ones resolved
#: against the assumption's current row
#: (docs/superpowers/specs/2026-09-25-a-decided-assumption-is-not-left-with-you.md).
_AUTOREVIEW_OWED_BY_USER = ("autoreview_escalated", "autoreview_unconfirmed")


def autoreview_state(store: ProjectStore, wo: dict[str, Any]) -> dict[str, Any] | None:
    """What `jarvis wo show` and the dashboard say about the automatic review, or None.

    `automerge_state`'s shape and its reasons, one authority along — None on every work
    order the mechanism never touched, and the NEWEST EVENT WINS BY TIMESTAMP rather than
    by a fixed kind order, because a work order can accrue several of these: three
    assumptions can be asked about, two accepted and one escalated, and the line must say
    where that left things rather than which kind was checked first.

    NO TERMINAL KIND, unlike its sibling. An `autoreview_accepted` is terminal for ONE
    assumption and says nothing about the next, so a work order whose last event is an
    escalation is held even though an acceptance came earlier — which is exactly what the
    timestamp already says.

    **This is the SUMMARY line, and it is not where the attribution lives.** Who decided
    each assumption is on the assumption row itself (`assumptions.decided_by`), which is
    what every surface listing them reads; this says what the mechanism did last.
    """
    from . import db

    newest: dict[str, Any] | None = None
    for kind in AUTOREVIEW_EVENTS:
        rows = store.events_of_kind(wo["id"], kind)
        if not rows:
            continue
        # Payload first, so `kind` and `ts` are this function's answers and not whatever
        # an event happened to carry under those names (`automerge_state`'s note).
        candidate = {**db.from_json(rows[-1]["payload"], {}),
                     "kind": kind, "ts": float(rows[-1]["ts"])}
        if newest is None or candidate["ts"] > newest["ts"]:
            newest = candidate
    if newest is None:
        return None
    # "left with you" is a claim about the PRESENT: resolve it against the row, once
    # (2026-09-25-a-decided-assumption-is-not-left-with-you.md §1).
    if newest["kind"] in _AUTOREVIEW_OWED_BY_USER:
        aid = int(newest.get("assumption_id") or 0)
        row = store.get_assumption(aid) if aid else None
        status = str((row or {}).get("status") or "")
        if status and status != "pending":
            newest = {**newest, "resolved_status": status,
                      "resolved_decider": assumption_decider(row or {})}
    return {**newest, "line": _autoreview_line(newest)}


def assumption_decider(a: dict[str, Any]) -> str:
    """WHO decided this assumption, in words, for whoever is about to read the verdict.

    THE POST-CONDITION THIS EXISTS FOR: a machine decision must never read as the user's.
    This is the ONE place that phrase is built. `jarvis wo show` reaches it through
    `assumption_line`; the work-order page calls it directly, because the page wants the
    attribution inside a badge and the rest of the row in its own cells. The LAYOUTS
    differ and that is fine — the attribution must not, because two spellings of it are
    two chances for one to drop the "the OS" and credit a machine verdict to the reader.

    `''` means the user (`ASSUMPTION_DECIDER_USER`'s note): every row written before the
    column existed was, by construction, the user's.

    AN UNSETTLED ASSUMPTION CARRYING A PROVISIONAL VERDICT HAS NO DECIDER, and saying
    "you" there would be the exact inversion this function exists to stop: the only thing
    that has judged it is a model, and it judged an intention. The phrase names the
    machine and says out loud that nothing is settled — still from this one renderer, so
    the surfaces in §8 of the early-review spec cannot spell it two ways.
    """
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


#: HOW FINAL AN `autoreview` EVENT IS about the assumption it names, for ties on `ts`.
#: The ties are real, not defensive: `Daemon._deliver_assumption_verdict` writes a hold
#: and an escalation in one pass, and the escalation is the one that says where the
#: assumption ended up.
#: The order is the LIFECYCLE, so a tie resolves to the later stage: asked, then judged
#: provisionally, then objected, then withdrawn — and the delivery pass (§7), which is the
#: only thing that settles, outranks everything the early pass said about an intention.
#: Every kind in `AUTOREVIEW_EVENTS` needs an entry; `assumptions_with_rulings` indexes
#: this dict directly and a missing one is a KeyError on the page.
_RULING_RANK = {"autoreview_confirmed": 9, "autoreview_unconfirmed": 8,
                "autoreview_accepted": 7, "autoreview_escalated": 6,
                "autoreview_held": 5, "autoreview_objection_withdrawn": 4,
                "autoreview_objected": 3, "autoreview_provisional": 2,
                "autoreview_asked": 1}


def assumptions_with_rulings(store: ProjectStore, wo_id: str) -> list[dict[str, Any]]:
    """`all_assumptions`, each row carrying what the OS DID with it under `os_ruling`.

    THE ONE DERIVATION, because the hold reason lives nowhere else. `autoreview_held` is
    a timeline event and not a column (`Daemon._note_autoreview_held`), so a surface that
    wants to say "held — mentions 'production'" has to read the events, and two surfaces
    reading them apart is two chances to render a reviewed assumption as untouched —
    GitHub issue #712. `jarvis wo show` and the work-order page take their rows from
    here and render them with `assumption_ruling_line`.

    NEWEST EVENT PER ASSUMPTION, never per work order: `autoreview_state`'s one line is
    the mechanism's last act and says nothing about the assumption beside it.

    IT IS ALSO THE ONE ROW-ENRICHMENT POINT, and that is why the two facts §8 needs but
    the row cannot hold — `objection_undeliverable` and `objection_response` — are
    attached here: `jarvis wo show` (cli.py) and the work-order page (ui/app.py) both take
    their rows from this function, so a fact derived here reaches both surfaces and a
    fact derived on one of them reaches one.
    """
    from . import db

    newest: dict[int, dict[str, Any]] = {}
    for kind in AUTOREVIEW_EVENTS:
        for event in store.events_of_kind(wo_id, kind):
            payload = db.from_json(event["payload"], {})
            aid = int(payload.get("assumption_id") or 0)
            if not aid:
                continue
            # Payload first, so `kind` and `ts` are this function's own answers —
            # `autoreview_state`'s note.
            candidate = {**payload, "kind": kind, "ts": float(event["ts"])}
            prev = newest.get(aid)
            if prev is None or ((candidate["ts"], _RULING_RANK[kind])
                                > (prev["ts"], _RULING_RANK[prev["kind"]])):
                newest[aid] = candidate
    base = store.all_assumptions(wo_id)
    rows = [{**a, "os_ruling": newest.get(int(a.get("id") or 0)),
             # Both derivations read the carrier and the timeline, so they are asked only
             # of the rows that carry an objection at all — which is nearly none, and is
             # why a project that never objected pays nothing for this. The undeliverable
             # verdict is NOT derived here: §9's `invariants.objection_undeliverable` is
             # the derivation that landed, and attention and this view must never be able
             # to disagree about whether a transport failed.
             "objection_undeliverable": (bool(a.get("objection_envelope_id"))
                                         and invariants.objection_undeliverable(store, a)),
             "objection_response": (objection_response(store, a, rows=base)
                                    if a.get("objection_delivered_ts") else None)}
            for a in base]
    # Children of this plan that have already landed, for the rows that are still the
    # user's. Only when something is pending and this order is a planner, so every other
    # call costs nothing (spec 2026-09-24-a-planner-assumption-holds-its-feature §2.6).
    over = (_overtaken(store, wo_id)
            if any(str(a.get("status") or "") == "pending" for a in rows) else None)
    return [{**a, "overtaken": over if str(a.get("status") or "") == "pending" else None}
            for a in rows]


def _overtaken(store: ProjectStore, wo_id: str) -> dict[str, int] | None:
    """`{"merged": n, "of": m}` when children of the plan this planner produced have
    already landed, else None.

    From the TIMELINE, never from `work_orders.pr_state` — kn-dbc4971d, that column is
    stale by construction. `pr_merged` is written by `complete_merged`, the single
    close-out for a hand-merge and an auto-merge alike, so one read covers both routes.
    """
    fo = store.feature_order_for_planner(wo_id)
    if fo is None or fo.get("plan_wo_id") != wo_id:
        return None
    children = store.feature_children(fo["id"])
    merged = sum(1 for c in children if store.events_of_kind(c["id"], "pr_merged"))
    return {"merged": merged, "of": len(children)} if merged else None


#: The acts that count as THE WORKER ANSWERING an objection, and the verb each is rendered
#: with. A daemon-written event is not among them and must never be: an OS event after the
#: delivery proves nothing about the worker, and reading one as an answer would silently
#: retire the "waiting on the worker" line §8 owns.
_WORKER_ACT_VERB = {"assumption": "the worker recorded another assumption",
                    "message": "the worker replied",
                    "question_asked": "the worker asked Neo",
                    "finished": "the worker finished",
                    "abandoned": "the worker abandoned the order"}


def objection_response(store: ProjectStore, a: dict[str, Any], *,
                       rows: list[dict[str, Any]] | None = None) -> dict[str, Any] | None:
    """THE FIRST THING THE WORKER DID after the objection reached it, or None.

    §6.5: nothing is stored for this, it is read forward from `objection_delivered_ts`
    over the record that already exists. None means the objection was never delivered, or
    it was and the worker has not acted yet — which `objection_response_line` renders as
    the "waiting on the worker since …" line.

    FOUR ACTS AND ONLY FOUR (`_WORKER_ACT_VERB`): a later assumption, a message the
    worker sent (`agent_to_user`), a question it asked Neo, or its finishing/abandoning
    the order. `rows` lets the caller pass the assumptions it has already read.
    """
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
    # THE FIRST act, not the last: the question is what the worker did about the
    # objection, and everything after that is the rest of its run.
    return min(acts, key=lambda act: act["ts"])


def objection_response_line(a: dict[str, Any]) -> str:
    """WHAT THE WORKER DID about the objection, or that it still owes an answer. Or `''`.

    Pure and public for `provisional_line`'s reason: §8 puts this fact on `jarvis wo show`
    and on the work-order page, and §8 owns the "waiting on the worker since …" sentence
    outright — `invariants.true_blockers` returns only what the USER owes, and this is the
    one state in the feature owed by somebody else (§9).

    `''` is every row whose objection was never delivered: in flight, withdrawn, and
    every historical row. A worker cannot owe an answer to a message it never received.
    """
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
    """WHAT THE OS DID with this assumption, in one line, or `''`. Pure, both surfaces.

    Not the attribution — that is `assumption_decider`'s, and both surfaces already
    render it. This is the REASONING beside it: the escalation the user has to answer,
    the keyword hold, the question still out, or why a settled one was settled. `''`
    means nothing has looked at this assumption, which is a different fact from a hold
    and has to read as one.

    The `#N ` prefix comes off a hold reason: the caller has just printed the number.
    """
    if str(a.get("status") or "") != "pending":
        return str(a.get("decided_reason") or "").strip()
    ruling = a.get("os_ruling") or {}
    kind, reason = str(ruling.get("kind") or ""), str(ruling.get("reason") or "").strip()
    question = ruling.get("neo_question_id")
    if kind == "autoreview_escalated":
        asked = f" (question {question})" if question else ""
        return f"Neo escalated{asked}{': ' + reason if reason else ''}"
    if kind == "autoreview_held":
        held = re.sub(r"^assumption #\d+ ", "", reason)
        return f"Held by the OS — {held}" if held else "Held by the OS"
    if kind == "autoreview_asked":
        return f"Asked Neo (question {question}), awaiting ruling"
    if kind == "autoreview_unconfirmed":
        # The early reading did not survive the diff, so the assumption is the user's and
        # they need the sentence that says why — the same shape as an escalation, which
        # is what this is. The early reading itself is `provisional_line`'s.
        asked = f" (question {question})" if question else ""
        return (f"Neo did not confirm its early reading{asked}"
                f"{': ' + reason if reason else ''}")
    # Every other early-pass kind is already on the ROW — `provisional_line` and
    # `objection_line` read columns, which outlive an event and cannot go stale.
    return ""


def overtaken_line(a: dict[str, Any]) -> str:
    """CHILDREN OF THIS PLAN THAT ALREADY MERGED, in one line, or `''`. Pure, both
    surfaces.

    The fact the user needs in order to rule, never a ruling: the assumption stays
    pending, and what changes is what a rejection now costs them (spec §2.6).
    """
    over = a.get("overtaken") or {}
    if not over:
        return ""
    return (f"{over['merged']} of {over['of']} children have already merged — rejecting "
            f"this now means a follow-up fix, not an unwind")


def _stamp(ts: Any) -> str:
    """A timestamp a person can read. Local time, minutes: seconds tell nobody anything."""
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(float(ts)))


def provisional_line(a: dict[str, Any]) -> str:
    """NEO'S EARLY READING of this assumption, in one line, or `''`.

    Public and pure, for `assumption_line`'s reason: §8 of the early-review spec puts the
    same fact on `jarvis wo show` and on the work-order page, and a fact spelled twice is
    a fact that eventually disagrees with itself. `''` is every row nothing judged early
    — every historical row, and every row in a project with early review off — and it
    must render exactly as it did before this shipped.

    It says PROVISIONALLY out loud on the accept side. An early verdict is an opinion
    about an intention and settles nothing (§4.1); a reader who takes it for a decision
    would think their gate had been opened when it has not.
    """
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
    """THE OBJECTION SENT TO THE WORKER: how it travelled, and how it ended. Or `''`.

    FOUR END STATES AND THEY ARE FOUR DIFFERENT THINGS TO A USER — in flight, delivered,
    withdrawn because the order stopped first, and undeliverable — so rendering any two
    of them the same is the defect this line exists to avoid (§8). Every objection the OS
    ever sent is visible here, delivered or not: that is the user's stated requirement.

    `objection_undeliverable` is the one fact NOT on the assumption row: §9 defines it
    over the carrier (the message `failed`, or the envelope terminal and not delivered),
    which takes two more reads. A caller that has done them passes the verdict in on the
    row; one that has not gets "in flight", which is what the row alone can honestly say.
    """
    if not a.get("objection_envelope_id"):
        return ""
    transport = str(a.get("objection_transport") or "") or "queue"
    sent = f" at {_stamp(a['objection_sent_ts'])}" if a.get("objection_sent_ts") else ""
    head = f"objection sent to the worker over the {transport}{sent}"
    if a.get("objection_delivered_ts"):
        return f"{head}, delivered at {_stamp(a['objection_delivered_ts'])}"
    if a.get("objection_withdrawn_ts"):
        return (f"{head}, withdrawn at {_stamp(a['objection_withdrawn_ts'])} — the "
                f"order stopped before it could be delivered")
    if a.get("objection_undeliverable"):
        return f"{head}, UNDELIVERABLE — the worker was never told"
    return f"{head}, in flight"


def assumption_line(a: dict[str, Any]) -> str:
    """One assumption on one line, for `jarvis wo show`. Attribution from one renderer.

    The early pass's facts ride on the PENDING branch and nowhere else, which is the
    whole shape of the feature: a provisional verdict leaves the user owing a decision,
    so the line still opens by saying so.
    """
    n, status = a.get("n"), str(a.get("status") or "")
    content = str(a.get("content") or "")
    if status == "pending":
        parts = [p for p in (assumption_ruling_line(a), provisional_line(a),
                             objection_line(a), objection_response_line(a),
                             overtaken_line(a)) if p]
        return (f"#{n} pending your review: {content}"
                f"{' — ' + '; '.join(parts) if parts else ''}")
    reason = str(a.get("decided_reason") or "").strip()
    # The early reading stays on a SETTLED row too: what the OS thought while the work
    # ran and what it thought once it saw the result are two readings, and §7 hands the
    # user both when they disagree.
    tail = "; ".join(p for p in (provisional_line(a), objection_line(a),
                                 objection_response_line(a)) if p)
    return (f"#{n} {status} by {assumption_decider(a)}"
            f"{' — ' + reason if reason else ''}: {content}"
            f"{' — ' + tail if tail else ''}")


def _autoreview_line(state: dict[str, Any]) -> str:
    """One line. The verb says WHO acted, which is the whole point of the record."""
    kind = state["kind"]
    n = state.get("n")
    # One shared suffix: both "left with you" branches make the same claim
    # (2026-09-25-a-decided-assumption-is-not-left-with-you.md §2).
    since = (f"; since {state['resolved_status']} by {state['resolved_decider']}"
             if state.get("resolved_status") else "")
    if kind == "autoreview_accepted":
        return (f"assumption #{n} accepted by the OS (Neo, "
                f"{state.get('model') or 'model not recorded'}) — "
                f"{state.get('reason') or 'no reason recorded'}")
    if kind == "autoreview_escalated":
        return (f"assumption #{n} left with you — "
                f"{state.get('reason') or 'no reason recorded'}{since}")
    if kind == "autoreview_asked":
        return f"assumption #{n} is with Neo (question {state.get('neo_question_id')})"
    # The early pass's five. Each gets a branch rather than falling through, because the
    # fallthrough below says "held" — a kind with no branch would claim the OS refused to
    # judge an assumption it had in fact judged.
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
    """One line, in the words the spec's §5.4 chose. The verb says who acted."""
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
    return f"held — {state.get('reason') or 'no reason recorded'}"


def validation_detail(store: ProjectStore, *, wo_id: str | None = None,
                      fo_id: str | None = None) -> dict[str, Any]:
    """THE DELIBERATION on one unit, in full: every round with every seat.

    The on-demand half of the separation every default surface keeps — each seat's
    verdict, status, model, latency and raw reply, plus the envelopes the review
    feedback travelled in. Nothing in it is pushed at anyone: `jarvis validation show`
    asks for it explicitly, and the two dashboard pages fold it shut.

    One function serves both units because a round is the same fact either way. It takes
    an open store so the pages that already have one do not open a second, and so the
    CLI and the dashboard cannot drift about what a deliberation contains.
    """
    # `validation_rounds` is a DIFFERENT projection, and this one is what the dashboard
    # macro and `jarvis validation show` are fed — so the key is added in both, off the
    # same resolver, because two surfaces computing it separately is how they come to
    # show different things (`kn-99e37a4b`).
    filed = filed_follow_ups(store, wo_id=wo_id, fo_id=fo_id)
    rounds = [{**rnd, "opinions": store.validation_opinions(rnd["id"]),
               "follow_ups": filed.get(int(rnd["id"]), NO_FOLLOW_UPS)}
              for rnd in store.validation_rounds(wo_id=wo_id, fo_id=fo_id)]
    envelopes = store.envelopes(subject_wo_id=wo_id, subject_fo_id=fo_id)
    return {"rounds": rounds, "envelopes": envelopes,
            # Pulled out rather than left for every reader to filter: an undeliverable
            # envelope is a failure — feedback that reached nobody — and one that has to
            # be noticed by scanning a `state` column is one nobody notices.
            "undeliverable": [e for e in envelopes if e["state"] == "undeliverable"]}


def validation_view(unit_id: str, project_name: str | None = None) -> dict[str, Any]:
    """`validation_detail` for a unit named on the command line, either kind.

    Which unit is being asked about is read off the id — `fo-…` is a feature order,
    anything else a work order — so the caller never has to say, and one command can
    serve both.
    """
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
    finally:
        store.close()
    return {"project": name, "unit": unit, "id": unit_id,
            "title": row["title"], "status": row["status"], **detail}


def current_config_version() -> str | None:
    """The id of the configuration in force, or None when the ledger holds nothing.

    None is the honest answer on a fleet that has never written a version, and it is
    what every stamp written by this OS falls back to (config-console design §5).
    """
    central = CentralStore()
    try:
        return (central.head_config_version() or {}).get("id")
    finally:
        central.close()


def config_version_line(version_id: str | None) -> str:
    """One configuration stamp, as a person reads it: the id and how far behind it is.

    `not recorded` for NULL — the unit ran before the console existed, which is not
    version 1 and must never render as one. The count is what makes the id actionable:
    an id alone says nothing about whether the fleet has moved since.
    """
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


def validation_config(project: str | None = None) -> Any:
    """The validation settings in force for `project` — or the OS's — or None.

    `None` for the project means the OS-level answer, which is what a caller holding no
    project has always got. A named project gets its own resolved `ProjectSpec.validation`
    (design doc §1.2); an unknown name is a `CatalogError` and therefore answered None,
    not raised, for the same reason the rest of this is best-effort.

    Best-effort on purpose, and it is `os_status`'s pattern rather than a new one: a
    worker calling `jarvis wo finish` from a checkout whose catalog has moved, or with
    no catalog registered at all, must still be able to finish. No catalog means no
    validation, which is the shipped default anyway.
    """
    try:
        catalog = resolve_catalog()
        if project is None:
            return catalog.os.validation
        return catalog.project(project).validation
    except (OpsError, CatalogError, OSError, ValueError):
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

    **THE PULL REQUEST IS A URL HERE, NEVER A STATE.** Routing on `pr_state` instead was
    tried and reverted: that column is written only by `Daemon.poll_pull_requests`, which
    looks at `waiting_pr_merge` alone and never clears what it wrote — so a worker whose
    pull request was closed, who was sent back and who finished again behind a NEW one
    still carries `CLOSED`, and a landing that believed it would drop a live pull request
    out of the merge queue and close the backlog item under it. The one caller that may
    read that column is the one that can know it is current: see `review_work_order`.

    **AND `completed` NOW ASSERTS THE DELIVERABLE.** A work order with no pull request
    whose branch carries commits has produced code that is on nothing but that branch,
    and letting it complete is GitHub issue #232 — six orders, ~3,100 lines, found by a
    human going looking. Every route to `completed` passes through here, which is why
    the check is here and not in each of them. It PARKS rather than raising, because two
    of its three callers are the daemon's round machine and `review_work_order`, where
    an exception would break a tick or a user's command and neither has anyone who could
    act on it. `finish` has someone who can — the worker — so it refuses there instead,
    off the same predicate: see `unlanded_work`.
    """
    wo_id = wo["id"]
    pr_url = pr_url or wo.get("pr_url") or None
    if not pr_url and not store.work_abandoned(wo_id):
        stranding = unlanded_work(store, wo)
        if stranding.produced:
            return park_unlanded(store, wo, stranding)
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


def unlanded_work(store: ProjectStore, wo: dict[str, Any],
                  pr_url: str = "") -> landing.Authored:
    """What settling this work order right now would strand. Issue #232's whole predicate.

    Narrow on purpose, and the narrowness is the point (Neo question 280): commits or
    uncommitted files in the worktree, AND no pull request. An order that produced no
    code is invisible to it — the exclusion that keeps 60 planners, investigations and
    knowledge-base writes out of every report built on this, DERIVED from the worktree
    rather than listed as work-order kinds that would rot.

    **IT DOES NOT READ THE ABANDONMENT, and that is not an omission.** An abandonment
    excuses the settling it was written with, and each caller knows a different thing
    about which settling this is: `finish` holds THIS CALL's `--abandon` and must judge a
    second, ordinary finish afresh — the record cannot tell it apart, because the new
    `finished` event does not exist yet when the check runs. `land_finished` and the
    sweep run later, over a record that is complete, and read `work_abandoned`.

    **THE PULL REQUEST IS READ FROM THE RECORD, NEVER FROM `wo`.** `review_work_order`
    hands its landing a copy with `pr_url` deliberately BLANKED when the poll has already
    settled that pull request — which for a MERGED one means the work landed. Trusting
    the caller's dict there would refuse to complete a work order whose code is on the
    default branch, over the very commits that put it there: the "flags everything"
    failure this check has to avoid, arriving by the back door. `pr_url` is the argument
    for the opposite case, `finish --pr`, where the record has not been written yet.

    Deliberately NOT keyed on whether that pull request merged. The order that finished
    behind a merged pull request and then kept working is issue #232's Mode C, and it
    belongs to the sweep (`landing.assess`), which can afford to look at the repository
    properly.
    """
    from . import landing

    recorded = store.get_work_order(wo["id"])
    if pr_url or recorded.get("pr_url"):
        return landing.Authored()
    return authorship(store, wo)


def authorship(store: ProjectStore, wo: dict[str, Any]) -> landing.Authored:
    """What this work order's worktree has produced, asked WITHOUT the pull-request rule.

    `unlanded_work` is the refusal and short-circuits to "nothing" for an order carrying
    a pull request, which is right for a refusal and wrong for the record: an order that
    wrote code and landed it behind a PR still wrote code, and INV-PR-RECORDED needs that
    written down while the worktree still exists to say so. So the two are separated —
    this reads, `unlanded_work` judges — and no settling pays for two reads.
    """
    from . import landing

    recorded = store.get_work_order(wo["id"])
    return landing.authored(landing.worktree_of(store.project_path, recorded))


def unmerged_pull_request(store: ProjectStore, wo_id: str) -> str:
    """This order's recorded pull request, unless the record says it merged. "" if it did.

    The other half of `unlanded_work`, and the reason `jarvis wo done` can clear an
    INV-WORK-LANDED alert at all. `unlanded_work` reads the WORKTREE; that check reads the
    PULL REQUEST, and the two disagree the moment a worktree is cleaned up — which is
    every order old enough for the check to be complaining about it. Measured on the live
    `jarvis_os` records on 2026-09-18: five of the eight alerted orders had no worktree on
    disk, so `jarvis wo done` wrote no `work_unlanded` event, so the exclusion the check
    looks for was never recorded and the same alert came back on the next sweep. An alert
    whose printed remedy does nothing is how a checker gets switched off.

    "The record says it merged" is a `pr_merged` event or a settled `landing_seen` — never
    a round trip, because this runs inside a CLI command the user is waiting on. Nothing
    having looked yet reads as unmerged, which over-records rather than under-records: a
    `work_unlanded` event about a pull request that turns out to have merged excuses an
    order the check was already silent about, and `work_unlanded_open`'s episode
    arithmetic retires it the moment a `pr_merged` lands anyway.
    """
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
    """Hold a work order that would otherwise complete over unlanded code, and SAY SO.

    The event is the point. Issue #232's Mode B is a user closing a work order by
    accepting its assumptions, which completed it and wrote `result_summary` NULL and
    `pr_url` NULL over the fact that a commit existed — so the record afterwards said
    the order had produced nothing. This writes down what was there instead: the branch,
    the count, and the files that were never committed at all.

    **ONCE PER EPISODE, NOT ONCE PER CALLER.** `Daemon.settle_work_order` re-derives an
    order's ending from the LATEST turn on EVERY tick, so it reaches here again on the
    tick after a park, over the same done turn, with nothing changed — and an
    unconditional write would trade "unparks and completes" for a fresh `work_unlanded`
    and a fresh flag every tick, which is the renotify-on-every-restart shape the
    invariants module's third rule exists to forbid. `work_unlanded_open` is the episode
    test and already means exactly this: a park with no `finished`, `abandoned` or
    `pr_merged` since. A re-delivery writes one of those, so the NEXT park is a new
    episode and does record again.

    The STATUS is still asserted on the quiet path and the FLAG is deliberately not.
    `UNLANDED_BLOCKER` is one `true_blockers` re-derives from `work_unlanded_open`, so
    INV-ATTENTION-MISSING puts it back if it is genuinely missing — and that path honours
    `acknowledged_blockers`, which re-flagging here would silently overwrite. A user who
    ran `jarvis wo ack` over a parked order must not have the flag raised again by the
    next tick; that is the same renotify defect wearing the column instead of the event.
    """
    if store.work_unlanded_open(wo["id"]):
        store.update_work_order(wo["id"], status="needs_review")
        return "needs_review"
    store.add_event(wo["id"], "work_unlanded", {**work.record(), "was": wo["status"]})
    store.set_status(wo["id"], "needs_review")
    store.flag_attention(wo["id"], UNLANDED_BLOCKER)
    return "needs_review"


def land_when_cleared(store: ProjectStore, wo: dict[str, Any],
                      pr_url: str | None = None, *,
                      panel_cleared: bool = False,
                      panel_open: bool = False) -> str:
    """THE JOIN: where a finished work order sits, given BOTH gates over it.

    An assumption review and a validation round are two independent judgements over one
    artifact, opened together and joined here — never chained, which is what cost
    wo-4fc128ca 44 minutes of dead wait (GitHub issue 212, spec
    docs/superpowers/specs/2026-09-13-two-gates-not-a-chain.md §2). Every route that
    could end a work order goes through this one function, for the reason
    `land_finished` gives about its own two: they must not drift.

    **`needs_review` wins while an assumption is pending.** It is the only status that
    says the user owes a decision, and `validating` is deliberately silent
    (`invariants.BLOCKED_STATUSES`). The round keeps running underneath it and
    `invariants.status_label` says so.

    `panel_cleared` is for a caller that has just settled the panel's half ITSELF and
    must not re-read the round it wrote: the no-validator path closes its round `failed`
    — never `passed`, because nobody judged the work — and that outcome otherwise reads
    here as a round still in flight.

    **`panel_open` is its opposite and is the BOUNCE's**, which refused a submission
    without opening a round at all. The latest row is then not the row that decision was
    taken from — `unanswered_paths` reads `last_judged_round`, which walks back past
    `failed` and `void` rows — so re-reading it here could land, on a `void` or a
    `passed` older than the rejection, work a panel has just been told not to judge. The
    caller states the outcome it holds instead of this function inferring one. Both
    halves of the user's gate above still run: an assumption is a separate judgement and
    a bounce says nothing about it.
    """
    assert not (panel_cleared and panel_open), "the panel is settled or it is not"
    wo_id = wo["id"]
    if store.pending_assumptions(wo_id):
        store.set_status(wo_id, "needs_review")
        store.flag_attention(wo_id, "assumptions pending review")
        return "needs_review"
    if not refusal_answered(store, wo_id):
        # The flag and the guidance are `review_work_order`'s — this is the panel's
        # settle path arriving at a work order the user has since turned down, and all
        # it owes is not to land it.
        store.set_status(wo_id, "needs_review")
        return "needs_review"
    latest = store.latest_validation_round(wo_id=wo_id) if not panel_cleared else None
    outcome = str((latest or {}).get("outcome") or "")
    if panel_open or outcome in OPEN_VALIDATION_OUTCOMES:
        store.set_status(wo_id, "validating")
        store.clear_attention(wo_id)
        return "validating"
    # `escalated` lands. The panel gave up and put this in front of the user, and the
    # only caller that can reach here with one is `review_work_order` — the user saying
    # ship it anyway, which is the whole exit from a give-up. So does `void`, which is
    # the panel finding nothing a reviewer could add and settling the unit itself
    # (`Daemon._void`, which passes `panel_cleared` and so never re-reads it here).
    return land_finished(store, wo, pr_url)


def refusal_answered(store: ProjectStore, wo_id: str) -> bool:
    """Has the worker delivered again since the user last REFUSED an assumption?

    The other half of the user's gate, and the one "nothing is pending" misses: a
    refused assumption is guidance the worker has not answered, and a panel that passes
    the round in the meantime would land the very decision the user turned down. Only
    reachable because the two gates now run in parallel (spec §5).

    The boundary is the `finished` event, which is the same one
    `ProjectStore.review_assumption`'s round rule is read against (kn-82d853ca) — and
    why `finish` records it BEFORE it settles anything.
    """
    refusals = [e for e in store.events_of_kind(wo_id, "reviewed")
                if not db.from_json(e["payload"], {}).get("accepted", True)]
    if not refusals:
        return True
    delivered = store.events_of_kind(wo_id, "finished")
    return bool(delivered) and float(delivered[-1]["ts"]) > float(refusals[-1]["ts"])


#: HOW MANY TIMES RUNNING a submitter may be sent back without the panel before the OS
#: stops trying and asks the user. Two, because the bounce is free and a free loop with
#: no ceiling is the failure this whole feature is about — spec
#: docs/superpowers/specs/2026-09-22-a-round-must-answer-the-list.md §6.
BOUNCE_LIMIT = 2

#: WHAT A BOUNCED SUBMITTER IS TOLD. Deliberately shaped like `daemon.REVIEW_FEEDBACK`
#: and deliberately NOT numbered "round n of max": no round was opened and none was
#: spent, and a number here would teach the submitter it has fewer attempts than it has.
BOUNCE_FEEDBACK = """REVIEW FEEDBACK (no round was spent)
Round {n} asked you to change {paths}, and this submission changes none of them. It was
not sent to the panel: a resubmission that answers nothing on the list costs a review
round and moves the work order no further.

Go back to round {n}'s feedback and address it. If you believe one of those points is
already answered, or should not be, say which and why in `--evidence` and change the
file it is about anyway — the review cannot read an argument you did not make in the
diff. Then run `jarvis wo finish {wo_id} --summary "..." --evidence "..."` again."""

#: The give-up when that has happened `BOUNCE_LIMIT` times running.
BOUNCE_EXHAUSTED = (
    "this work order has been sent back {n} times running for changing nothing round "
    "{round} asked about ({paths}), and it is still changing none of them. Nobody has "
    "judged the latest submission: decide whether the review's list is right before "
    "spending another round on it.")


def escalate_validation_round(store: ProjectStore, wo: dict[str, Any], round_id: int,
                              n: int, reason: str) -> None:
    """Give up on this unit and ask the user. THE ONE HOME of that transition.

    Called by `Daemon._escalate`, whose docstring carries the reasoning for every line
    below — why the attention reason is `VALIDATION_STUCK_BLOCKER` verbatim, why the
    notification is the half the flag cannot do, and why Neo is not asked first.
    """
    from .daemon import VALIDATION_ESCALATED_TITLE, escalation_body
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


def unanswered_paths(store: ProjectStore, wo_id: str,
                     packet: Any) -> tuple[dict[str, Any], tuple[str, ...]] | None:
    """`(the round that asked, the paths it asked about)` when this submission answers
    none of them, else None.

    The store half of `validation.unanswered_submission`, which is pure and is where the
    rule lives. Two reads: the last round a panel settled, and the blockers it raised —
    read through `validation.blockers(validation.findings(...))`, the SAME pair
    `prior_round_history` reads, because a second classifier is a second answer to what
    the submitter was told.
    """
    from . import validation

    previous = store.last_judged_round(wo_id=wo_id)
    if previous is None:
        return None
    raised: list[dict[str, str]] = []
    for op in store.validation_opinions(int(previous["id"])):
        raised += validation.blockers(validation.findings(op))
    cited = validation.unanswered_submission(
        previous, raised, ProjectStore.validation_file_shas(previous),
        dict(packet.file_shas))
    return (previous, cited) if cited is not None else None


def consecutive_bounces(store: ProjectStore, wo_id: str, after_round: int) -> int:
    """How many times running this submitter has already been bounced off `after_round`.

    Keyed on the round it was bounced AFTER rather than counted raw, which is what makes
    "consecutive" true by construction: a panel round that happens in between becomes the
    new `after_round`, so the count restarts without anything having to reset it.
    """
    return sum(1 for e in store.events_of_kind(wo_id, "validation_bounced")
               if int(db.from_json(e["payload"], {}).get("after_round") or 0)
               == after_round)


def submit_for_validation(store: ProjectStore, project_path: Path, wo: dict[str, Any],
                          *, declared: str, cfg: Any,
                          forced_reason: str = "") -> dict[str, Any] | None:
    """Open a validation round over what this work order has produced, or bounce it.

    Collects the evidence, fingerprints it, opens the round and parks the work order in
    `validating`. It judges nothing: the daemon runs the validator off its tick thread
    and settles what comes back.

    **The round number is COUNTED, not derived from the row count** — a submission that
    is retried while its round is still open, or one that follows a transport outage,
    reuses the number it already has. The insert is idempotent per (work order, round),
    so two callers racing here produce one round rather than two.

    `forced_reason` is `force_validation`'s and nobody else's: it is stamped on the round
    and changes NOTHING about how the round is numbered, judged or settled. A forced
    round spends a number exactly like a submitted one, which is why the budget question
    has the answer it has — spec
    docs/superpowers/specs/2026-09-15-forcing-a-validation-round.md §4.

    **RETURNS None WHEN THE SUBMISSION WAS BOUNCED** — sent straight back to the worker
    because it changes nothing the previous round asked about, WITHOUT opening a round
    and so without spending one (spec
    docs/superpowers/specs/2026-09-22-a-round-must-answer-the-list.md §5). The work order
    parks in `validating` exactly as it does after a panel rejection, and
    `Daemon._deliver` flips it back to `running` when the envelope lands — but the caller
    MUST say so (`land_when_cleared(panel_open=True)`) rather than let the join re-derive
    it: the rejection this bounce read is `last_judged_round`'s, and the LATEST row can be
    a `failed` or `void` written after it. HERE rather than in the daemon because
    a round the daemon refuses has already been NUMBERED, and the number and the budget
    are one function (kn-a5e91633) — a bounce that spent a number would spend a round.

    A FORCED ROUND IS NEVER BOUNCED, for `Daemon._repeat_submission`'s reason: there is
    no submitter to send anything back to.

    THE COMPLETE CALLER SET, because `None` is a new answer they all have to be able to
    meet: `finish` (the only one that can see it, and the one that passes `panel_open`);
    `force_validation` and `rejudge_for_head`, both forced, so both still get a row; and
    `_land_after_acceptance`, which submits only through `_validates_on_review` — i.e.
    only when there is NO round on record — and a bounce needs a previous judged round,
    so that path cannot reach one either. A new caller joins that contract or handles
    `None`.
    """
    from . import evidence as evidence_mod
    from . import specs

    packet = evidence_mod.collect_work_order(
        project_path, wo, declared=declared, diff_chars=cfg.diff_chars,
        spec=specs.spec_of(store, wo), side_effects=side_effects_of(store, str(wo["id"])),
        # The store read is the caller's: `evidence` may not touch a database (spec §4).
        assumptions=store.all_assumptions(wo["id"]))
    nxt = store.counted_validation_rounds(wo_id=wo["id"]) + 1
    unanswered = (None if forced_reason.strip()
                  else unanswered_paths(store, str(wo["id"]), packet))
    if unanswered is not None:
        previous, cited = unanswered
        if consecutive_bounces(store, str(wo["id"]),
                               int(previous["round"])) < BOUNCE_LIMIT:
            _bounce(store, wo, previous, cited, nxt)
            return None
        # The ceiling. A round IS opened and immediately given up on, rather than the
        # give-up being written bare: `invariants.true_blockers` re-derives
        # VALIDATION_STUCK_BLOCKER from a round whose outcome is `escalated`, so an
        # escalation with no round behind it would have its attention flag rewritten on
        # the next reconcile tick and the user would never be asked.
        round_row = store.open_validation_round(
            wo_id=wo["id"], fingerprint=evidence_mod.fingerprint(packet),
            summary=str(wo.get("result_summary") or ""), evidence=declared,
            pr_url=wo.get("pr_url"), round=nxt,
            config_version=current_config_version())
        store.set_validation_file_shas(int(round_row["id"]), packet.file_shas)
        escalate_validation_round(
            store, wo, int(round_row["id"]), nxt,
            BOUNCE_EXHAUSTED.format(n=BOUNCE_LIMIT, round=int(previous["round"]),
                                    paths=", ".join(cited)))
        # RE-READ, because the row above is the one that was opened and the caller has to
        # be able to see that it has already been settled. `finish` reads this outcome to
        # know not to land the work order: `land_when_cleared` LANDS an `escalated` round
        # — deliberately, for `review_work_order` — and would put the give-up the user has
        # just been asked about straight into the merge queue.
        return store.get_validation_round(int(round_row["id"]))
    round_row = store.open_validation_round(
        wo_id=wo["id"], fingerprint=evidence_mod.fingerprint(packet),
        summary=str(wo.get("result_summary") or ""), evidence=declared,
        pr_url=wo.get("pr_url"), round=nxt,
        config_version=current_config_version(), forced_reason=forced_reason)
    store.set_status(wo["id"], "validating")
    # No attention flag: a unit under review is the system working. Only the give-up
    # transition flags anyone.
    store.clear_attention(wo["id"])
    store.add_event(wo["id"], "validation_submitted",
                    {"round": round_row["round"], "round_id": round_row["id"],
                     "fingerprint": round_row["fingerprint"],
                     "files": len(packet.files)})
    return round_row


def _bounce(store: ProjectStore, wo: dict[str, Any], previous: dict[str, Any],
            cited: tuple[str, ...], would_be: int) -> None:
    """Send this submission back to the worker without convening the panel.

    THE EVENT IS NOT OPTIONAL (the user's ruling, Neo question 518): a bounce that left
    no trace would be the OS silently discarding a delivery, and the paths it checked are
    the whole of why it did — a reader who disagrees can see the list, and the round it
    came from, without re-running anything.

    Written BEFORE the envelope, so a bus that refuses still leaves the record saying
    what happened. The feedback travels to the ROLE `implementor` and never to a named
    work order, for `Daemon._reject`'s reason.
    """
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


def force_validation_refusal(store: ProjectStore, wo: dict[str, Any], *,
                             project: str, cfg: Any) -> str | None:
    """Why a round CANNOT be forced on this work order, in the sentence that refuses it —
    or None when one can.

    THE ONE HOME OF THE RULE, because two surfaces apply it: `force_validation` raises
    whatever this returns, and the dashboard renders its control disabled beside it, so
    the user learns the rule from the page instead of from a failed submit. Two carefully
    written copies of a predicate pass every behavioural test and drift anyway
    (kn-4ea33fe6) — the sharing has to be structural, which is why the page calls this
    rather than checking a status tuple of its own.

    It takes an OPEN STORE and an already-resolved `cfg` so a page that holds both does
    not open a second store to ask, and it writes nothing: asking is free, and a surface
    that had to attempt the write to learn the answer is the surface this replaces.

    Order is the order a reader can act in — the project switch, then what the order IS,
    then what it has, then what is already running over it. It decides only which
    sentence a work order with two problems is given.
    """
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
    # ONE READ, predicate and wording derived from it (kn-08f2ff9b): the panel opens
    # rounds on its own thread, and two reads can straddle one — refusing while naming a
    # round that has since settled, or allowing over a round that has since opened.
    # `round_machine_owns` is the round machine's own definition of "this is mine", and a
    # round it still owns is one about to run or be retried: a second round underneath
    # would take the MAX-round slot out from under it.
    latest = store.latest_validation_round(wo_id=wo_id)
    if ProjectStore.round_machine_owns(latest):
        assert latest is not None  # `round_machine_owns` is False for None
        return (f"round {latest['round']} on {wo_id} is {latest['outcome']} — the panel "
                f"has not finished with it. Wait for the verdict; forcing a round now "
                f"would judge the same pull request twice")
    return None


def force_validation(wo_id: str, *, reason: str,
                     project_name: str | None = None) -> dict[str, Any]:
    """`jarvis validation force` — a PERSON opening a fresh round, with no worker.

    Why it exists: jarvis-0.10.0 introduced `validation_rounds.head_sha`, so every round
    judged before it carries `''` and `automerge.decide`'s condition 4 holds those work
    orders on `sha_unrecorded` for ever. The only route to a fresh round was `jarvis wo
    finish`, which writes a `finished` event and demands a `--summary` — so re-judging a
    work order meant forging a worker's account of work no worker had done. This reaches
    `submit_for_validation` directly and writes NO `finished` event; the packet is
    re-collected from scratch, which is how the new round comes to read the CURRENT pull
    request and record the CURRENT head.

    **`--reason` is REQUIRED and stored on the round**, following `jarvis config set` and
    `jarvis gate approve`. A forced round that looked organic afterwards is the defect
    this command exists to remove, so the reason is on `validation_rounds.forced_reason`
    — beside the verdict it caused, where `round_line` renders it — and on a
    `validation_forced` timeline event.

    **IT SPENDS A ROUND NUMBER LIKE ANY OTHER, AND `max_rounds` DOES NOT HOLD IT OFF**
    (Neo, question 300). Nothing about opening a round consults `cfg.max_rounds`: it is a
    settle-time branch in `Daemon._validate_work_order`, where `rejected` below the budget
    sends the worker feedback and `rejected` at or past it escalates to the user. That is
    the right landing for a round a person forced — the motivating population is parked
    work orders with no worker left to send feedback to — so the budget is left alone
    rather than exempted. It cannot be exempted cheaply either: `counted_validation_rounds`
    both counts rounds AND numbers them, so a round excluded from the count would have its
    number reused, hit the idempotent insert in `open_validation_round` and hand the
    caller an already-closed round while the work order parked in `validating` for ever.

    **WHAT IT ALLOWS is `FORCEABLE_STATUSES`, and that is a narrower claim than "what it
    does not refuse".** Only `waiting_pr_merge` and `needs_review`: a work order that has
    DELIVERED and whose worker is not typing. A `running` one would be judged while its
    worker is still writing to the branch, and — worse — an open round OWNS that worker's
    session (kn-01a4ab27), so `Daemon._reject` would post the panel's feedback into a
    session mid-task. The allowlist is the guard rather than a `running`-shaped refusal,
    so a status added to `WO_STATUSES` tomorrow is refused until somebody decides it is
    safe rather than allowed until somebody remembers it is not.

    The work order lands in `validating` and goes back to where the panel's verdict puts
    it: `waiting_pr_merge` on a pass (`land_when_cleared` re-parks it behind the still-open
    pull request, and the automatic merge can then arm on the head this round recorded),
    `needs_review` on an escalation.
    """
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
        # AFTER the round exists, so the event can name it. A person reading the timeline
        # sees who reopened this and why, next to the `validation_submitted` that a
        # submission would have written alone.
        store.add_event(wo_id, "validation_forced",
                        {"round": round_row["round"], "round_id": round_row["id"],
                         "reason": reason, "was": status})
        return {"project": name, "wo_id": wo_id, "round": round_row["round"],
                "round_id": round_row["id"], "reason": reason, "was": status,
                "status": store.get_work_order(wo_id)["status"]}
    finally:
        store.close()


#: The `forced_reason` an OS-forced round carries. It names BOTH commits, because the
#: sentence has to answer "why is there another round on this?" on its own, wherever it
#: is read — `jarvis validation show` renders it beside the verdict it caused.
REJUDGE_FORCED_REASON = ("the OS re-judged this itself: round {n} passed on {judged}, "
                         "and the head of the pull request is now {head} — nothing "
                         "merges a commit no round has read")

#: `validation_forced.by` for a round no person asked for. Absent means the person, which
#: is every row written before this existed and every `jarvis validation force` since.
REJUDGE_BY_OS = "os"


def rejudged_heads(store: ProjectStore, wo_id: str, *, declined: bool = False
                   ) -> set[str]:
    """The head commits the OS has already re-judged — or, with `declined`, the ones it
    has already refused to.

    THE DEDUPE, keyed on the COMMIT rather than on "has this ever happened" —
    `Daemon._note_automerge_held`'s key shape and kn-089de524's rule. A parked pull
    request is polled every couple of minutes, so a key that saturates would either spend
    a round per tick or stop re-judging a branch that genuinely moved again.

    TWO SETS AND NOT ONE, because they answer different questions and only one of them is
    permanent. A commit the OS has judged is judged for ever; a commit it DECLINED to
    judge was declined against a round budget, and the user may raise that budget — so
    the decline suppresses the repeated event and never the remedy.
    """
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


def rejudge_moved_head(store: ProjectStore, project_path: Path, wo: dict[str, Any], *,
                       project: str, cfg: Any,
                       decision: Any) -> dict[str, Any] | None:
    """The OS re-opening a round on a pull request whose head moved out from under its
    verdict. `force_validation`'s act, with a machine for an operator.

    Spec docs/superpowers/specs/2026-09-19-a-moved-head-re-judges-itself.md; the guards
    are its section 3 and the caller owns the two that need a `PullRequest`. Returns what
    happened — a round, or a decline — or None when a guard said not now, which is the
    overwhelmingly common answer and writes nothing.

    **IT GOES THROUGH `submit_for_validation` AND `force_validation_refusal`, NOT PAST
    THEM.** A second definition of "may a round be opened here" is a rule that holds by
    luck (`force_validation_refusal`'s own note), and the whole point of the round this
    opens is that it is indistinguishable from the one a person forces except in who
    asked for it.

    **THE LAST ROUND IS THE USER'S.** `max_rounds` is where a rejection stops going back
    to a worker and starts going to the user, and on a parked order there is no worker
    left — so a machine that spent the final round would hand the user an escalation
    instead of a decision they could have made with a round in hand. Declining is
    recorded rather than silent: `invariants.SHA_MOVED_BLOCKER` is derived from it. And
    it is not final: raising `max_rounds` makes the next tick re-judge the same commit,
    so the user's remedy for the one stall this cannot heal is a config change rather
    than a command they have to remember to run.
    """
    from . import worker_session

    wo_id = str(wo["id"])
    head = str(getattr(decision, "head_sha", "") or "")
    # No head to bind a verdict to — `gh` could not read one this tick. Nothing is
    # recorded: the dedupe key would be empty and would then swallow the real head.
    if not head or head in rejudged_heads(store, wo_id):
        return None
    if worker_session.busy(store, wo_id) or store.queued_messages(wo_id):
        return None
    if force_validation_refusal(store, wo, project=project, cfg=cfg) is not None:
        return None
    judged = str(getattr(decision, "judged_sha", "") or "")
    round_n = int(getattr(decision, "round_n", 0) or 0)
    nxt = store.counted_validation_rounds(wo_id=wo_id) + 1
    if nxt >= int(cfg.max_rounds):
        if head in rejudged_heads(store, wo_id, declined=True):
            return None                 # said once per commit, not once per tick
        store.add_event(wo_id, invariants.REJUDGE_DECLINED_EVENT,
                        {"head_sha": head, "judged_sha": judged, "round": round_n,
                         "next_round": nxt, "max_rounds": int(cfg.max_rounds)})
        return {"wo_id": wo_id, "declined": True, "head_sha": head,
                "judged_sha": judged, "next_round": nxt,
                "max_rounds": int(cfg.max_rounds)}
    was = str(wo["status"] or "")
    reason = REJUDGE_FORCED_REASON.format(n=round_n, judged=judged[:10],
                                          head=head[:10])
    round_row = submit_for_validation(store, project_path, wo,
                                      declared=declared_evidence(store, wo_id),
                                      cfg=cfg, forced_reason=reason)
    store.add_event(wo_id, "validation_forced",
                    {"round": round_row["round"], "round_id": round_row["id"],
                     "reason": reason, "was": was, "by": REJUDGE_BY_OS,
                     "head_sha": head, "judged_sha": judged})
    return {"wo_id": wo_id, "declined": False, "round": round_row["round"],
            "round_id": round_row["id"], "head_sha": head, "judged_sha": judged,
            "reason": reason, "was": was}


def force_validation_state(store: ProjectStore, wo: dict[str, Any], *, project: str,
                           held: dict[str, Any] | None) -> dict[str, Any] | None:
    """What the work-order page's "re-judge this pull request" control shows, or None.

    None — no control at all — when the validation panel is off for the project. A button
    for a mechanism a project never switched on is the noise `automerge_state` declines to
    print, one authority along. Every OTHER refusal renders the control DISABLED with its
    sentence, because those are rules about THIS work order and the page is where a person
    should learn them rather than by pressing a button.

    `held` is `automerge_state`'s answer, PASSED IN and never re-derived: the diagnosis is
    the hold `automerge.decide` already computed for this order, on the tick that declined
    to merge it. A second opinion derived here would need a `gh` call from a web request
    and would answer about a different moment than the record is describing
    (`automerge_state`'s own note).

    Only the two holds a fresh round CLEARS are diagnosed. A red build or a conflict is a
    hold this control cannot help with, and wording one of those as something to force a
    round over is how a user comes to spend round numbers on a failing CI run.
    """
    from . import automerge

    cfg = validation_config(project)
    if cfg is None or not cfg.enabled:
        return None
    refusal = force_validation_refusal(store, wo, project=project, cfg=cfg)
    state: dict[str, Any] = dict(held or {})
    if state.get("kind") != "automerge_held":
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
    """What forcing a round REPORTED, in two lines: the round it opened, and where the
    work order went.

    One formatter for the terminal and the page, over the dict `force_validation` returns
    — "it is `validating`" is only half an answer on either surface, and two renderings of
    one act are two chances to word it differently. The pointer at the verdict is NOT here:
    a terminal's is a command and a page's is a link, so each surface adds its own.
    """
    return [f"{result['wo_id']} [{result['project']}]: round {result['round']} opened by "
            f"hand — {result['reason']}",
            f"was {result['was']}, now {result['status']}; the panel judges it on the "
            f"daemon's next tick"]


def forced_round_notice(store: ProjectStore, wo: dict[str, Any], *, project: str,
                        round_n: int) -> list[str]:
    """`forced_round_lines` for a page that has just redirected after forcing a round.

    Rebuilt from the RECORD — the `validation_forced` event carries the reason and the
    status the order was in — rather than carried across the redirect, so nothing a
    visitor can type into the query string reaches the page as text. The round number only
    selects which event is read, and an unknown one renders nothing.
    """
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
    """What earlier rounds of this unit's review asked for, for `EvidencePacket.history`.

    Here rather than in `evidence` for `collect_feature_evidence`'s reason: it is two
    store reads per round, and that module may not touch a store.

    **BLOCKERS ONLY, and `validation.blockers(validation.findings(...))` is what decides
    which** — the same pair the chair's own prompt is built with, never a second reader.
    A reader that classified even slightly differently would put a follow-up into the
    shared prefix, which `_run_chair` hands the chair (spec
    docs/superpowers/specs/2026-09-15-the-panel-blocks-on-blockers.md §5.2.1).

    **Only the rounds the budget counted.** `pending` is a round in flight and `failed` is
    an outage or a panel that was never wired in, so its stored reason is about the OS
    rather than about the code — rendering either under "these points were raised" would
    tell five seats something false about the submission.

    A round whose opinions yield no blocker still earns an entry: the round row's own
    `outcome` and `reason` are what the submitter was actually told, and every opinion
    stored before this feature — and every one an injected validator writes — is prose
    that parses to no findings at all.
    """
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


def collect_feature_evidence(store: ProjectStore, project_path: Path,
                             fo: dict[str, Any], *, declared: str, summary: str,
                             cfg: Any,
                             history: Iterable[dict[str, Any]] = ()) -> Any:
    """The feature's packet, with each child's own account attached.

    Here rather than in `evidence` because assembling `children` is a store read per
    child, and that module may not touch a store — its whole value is that nothing it
    reports could have been influenced by the work it is reporting on.

    `history` is a PARAMETER and defaults to nothing on purpose: both callers pass
    through here, and only the daemon's — the packet the seats actually read — carries it.
    `submit_feature_for_validation` builds a packet to fingerprint, and `history` is
    excluded from that hash (spec 2026-09-15 §5.1).

    A child contributes what its OWN last validation round was told, not what it wrote
    into its pull request: that text was already judged once, so a feature seat comparing
    the integrated diff against it is re-checking the same claim at the only level where
    two children can contradict each other. A child that never validated contributes an
    empty string, which says so honestly.
    """
    from . import evidence as evidence_mod

    children = []
    for child in store.feature_children(fo["id"]):
        last = store.latest_validation_round(wo_id=child["id"])
        children.append({**child, "declared": str((last or {}).get("evidence") or "")})
    return evidence_mod.collect_feature(project_path, fo, children, declared=declared,
                                        summary=summary, diff_chars=cfg.diff_chars,
                                        side_effects=feature_side_effects(store,
                                                                          fo["id"]),
                                        history=history)


def submit_feature_for_validation(store: ProjectStore, project_path: Path,
                                  fo: dict[str, Any], *, declared: str, summary: str,
                                  cfg: Any) -> dict[str, Any]:
    """Open a validation round over the feature as a whole, and park it in `validating`.

    The mirror of `submit_for_validation`, and deliberately the same shape: collect,
    fingerprint, open the round by COUNTED number, park the unit, record the event. It
    judges nothing — the daemon runs the validator off its tick thread and settles what
    comes back.

    Two callers, one on each side of the loop: `Daemon.settle_features` opens round 1 when
    the last child lands, and `jarvis fo submit` opens every round after that. Neither
    reads the kill switch here; both read it before calling, which is the rule the whole
    design turns on (see `finish`).
    """
    from . import evidence as evidence_mod

    packet = collect_feature_evidence(store, project_path, fo, declared=declared,
                                      summary=summary, cfg=cfg)
    fo_id = fo["id"]
    nxt = store.counted_validation_rounds(fo_id=fo_id) + 1
    round_row = store.open_validation_round(
        fo_id=fo_id, fingerprint=evidence_mod.fingerprint(packet), summary=summary,
        evidence=declared, round=nxt, config_version=current_config_version())
    store.set_feature_status(fo_id, "validating")
    # No attention flag: a unit under review is the system working. Only the give-up
    # transition flags anyone — and for a feature order that flag goes on the feature.
    store.clear_feature_attention(fo_id)
    feature_event(store, fo_id, "validation_submitted",
                  {"round": round_row["round"], "round_id": round_row["id"],
                   "fingerprint": round_row["fingerprint"],
                   "files": len(packet.files), "feature_order": fo_id})
    return round_row


def feature_event(store: ProjectStore, fo_id: str, kind: str,
                  payload: dict[str, Any]) -> bool:
    """Write one feature-order event onto the timeline that carries it. True if it landed.

    A feature order has no timeline of its own — `wo_events.wo_id` is a real foreign key
    into `work_orders` — so every step of its life is recorded on whichever work order
    carried that step. For the validation loop that carrier is the PROJECT MANAGER order:
    it is the feature's only long-lived session, it is the addressee of everything this
    loop sends, and `jarvis wo show <manager>` is therefore where the round history reads
    back in order.

    False when the feature has no manager. That is not hypothetical — a plan released
    while `os.validation.enabled` was false has none, and the user can cancel one — and
    the caller must not treat a lost event as a written one: the round machine counts
    transport outages from these rows.

    Manager-only ON PURPOSE, and it is the NARROW case of
    `ProjectStore.carrier_for_feature`: this loop addresses the manager specifically, so
    falling back to a planner or a child would file a round on a session that is not the
    one being asked. Anything not addressed to the manager uses the general rule.
    """
    manager = store.manager_work_order(fo_id)
    if not manager:
        return False
    store.add_event(manager["id"], kind, payload)
    return True


def feature_events_of_kind(store: ProjectStore, fo_id: str,
                           kind: str) -> list[dict[str, Any]]:
    """Read back what `feature_event` wrote. Empty when the feature has no manager.

    Paired with the writer so that the carrier is decided in exactly one place: a counter
    reading the manager's timeline directly would keep working right up until the day the
    carrier changed, and then quietly count zero.
    """
    manager = store.manager_work_order(fo_id)
    return store.events_of_kind(manager["id"], kind) if manager else []


def submit_feature(fo_id: str, summary: str, evidence: str = "",
                   project_name: str | None = None) -> dict[str, Any]:
    """`jarvis fo submit` — the project manager saying the feature is ready again.

    A feature order runs no session, so it cannot finish itself the way a work order does.
    This is the manager's equivalent of `jarvis wo finish`: it opens the NEXT round over
    the integrated diff and hands the feature back to the panel.

    Only from `executing`, and the refusal is deliberately plain rather than an invitation
    to retry. A manager submitting a feature that is already `validating` is asking for a
    second opinion on a round in flight; one submitting a `completed` feature has misread
    its inbox. Both are told what the feature is doing instead.

    **The kill switch is read HERE**, at the submission site, exactly as `finish` reads
    it. With validation off — or with `feature_units` off — no round opens and the feature
    stays where it is; `Daemon.settle_features` then completes it as soon as its children
    are all done, which is the behaviour with the feature switched off entirely. That is
    what stops a manager waiting for a verdict nobody will ever produce.
    """
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
    """What the worker last said it did to test this, recovered from its own `finish`.

    A work order with pending assumptions never reaches `finish`'s validation branch, so
    without this the `--evidence` its worker declared would be dropped and
    `review_work_order` would open round 1 empty. The `finished` event is written on
    every route through `finish`, so its payload carries the text without a new column.

    The LAST one wins: a worker that finished, was sent back and finished again has
    superseded its earlier account.
    """
    for event in reversed(store.events_of_kind(wo_id, "finished")):
        text = db.from_json(event["payload"], {}).get("evidence")
        if text:
            return str(text)
    return ""


def gate_still_open(wo_id: str, request: dict[str, Any]) -> str:
    """Why this work order cannot settle yet, and the one way on from where it is."""
    from .gates import AWAITING_CASE, exits_advice

    if request["status"] == AWAITING_CASE:
        # Both exits, from the renderer every other block uses — a refusal that named
        # only the request would push a worker whose command performs no privileged
        # action into writing a false case (spec 2026-09-12-contesting-a-gate-match §3).
        way_on = ("Nobody is reviewing it: it carries neither a case nor a contest.\n\n"
                  + exits_advice(wo_id, request["command"], request["kind"],
                                 request["id"]))
    else:
        way_on = ("It is under review. End your turn — the verdict arrives as your "
                  "next user turn, and you finish from there.")
    return (f"{wo_id} has gate request {request['id']} ({request['kind']}) still open, "
            f"so it cannot be finished: settling it now would close the work order over "
            f"a privileged action nobody ruled on. {way_on}")


def unlanded_refusal(wo_id: str, summary: str, work: landing.Authored) -> str:
    """Why this work order may not finish, and the three ways on. Issue #232.

    The first branch is MODE A, and it is why the summary is read at all: four of the six
    stranded orders finished with a summary that NAMED a draft pull request in prose and
    passed no `--pr`, so `pr_url` stayed NULL and no poller ever watched it. The OS does
    not adopt that URL — it has verified nothing about it — but it can stop pretending it
    did not see it. Two of those four were recovered only because the user personally
    noticed and filed work orders titled "circle back on this PR .../pull/33".
    """
    from . import landing as landing_mod

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
    """The worker reporting its own result.

    `pr_url` is what separates "delivered" from "delivered and merged": a work order
    that ends in a pull request is not finished until a human merges it, so it settles
    into `waiting_pr_merge` and stays on the open list with the link, instead of going
    to `completed` and disappearing into the settled group nobody reads. The merge is
    what ends it: `Daemon.poll_pull_requests` watches the PR and completes the work
    order itself, and `jarvis wo done` remains the manual exit for a PR that will never
    merge.

    Pending assumptions still outrank it: those are a decision the OS is waiting on,
    and a PR the user merges before deciding them accepts them by the back door. They
    no longer outrank VALIDATION, though — the round opens here whether or not any are
    pending, and `land_when_cleared` joins the two (GitHub issue 212, spec
    docs/superpowers/specs/2026-09-13-two-gates-not-a-chain.md §1).

    `evidence` is the worker's own account of how it tested the change, and it is
    OPTIONAL: every worker in flight when this shipped predates the flag, so an empty
    one is an ordinary submission and not a thin one.

    **AND IT MAY NOT FINISH OVER CODE NOBODY HAS UNDERTAKEN TO LAND.** A worktree with
    commits and no `--pr` is GitHub issue #232, and `finish` is the moment the evidence
    is freshest and the one person who can act on it is listening. `abandon` is the way
    through and is meant to be cheap: an abandonment is a legitimate outcome, and the
    thing being enforced is that the decision is WRITTEN DOWN, not that it is prevented.
    The predicate is `unlanded_work`'s and is deliberately narrow — see Neo question 280.

    **AND IT WRITES DOWN WHAT THE ORDER AUTHORED, on every route including the one that
    settles cleanly.** That record is what INV-PR-RECORDED reads months later, when the
    worktree it came from is long gone; the refusal above is what keeps it honest.

    **`--abandon` ON AN ALREADY-SETTLED ORDER RECORDS AND STOPS.** It is INV-WORK-LANDED's
    printed remedy, so it is typed months after the work; opening a validation round there
    escalates a session that no longer exists and hands the user a fresh attention item in
    exchange for the one they just cleared. See the branch below.

    An open gate request outranks all of it, and this is the third enforcement point of
    docs/superpowers/specs/2026-09-12-a-gate-that-holds.md: declaring yourself done is
    the one route around a gate that neither the Stop hold nor the narrowed tool surface
    can close, because the command that takes it is a `jarvis …` contract command.

    **`os.validation.enabled` is read at the SUBMISSION SITES ONLY** — here and in
    `review_work_order`, the other route into done — and it gates OPENING a round and
    nothing else. A flag turned off while rounds are open must still let the daemon
    judge and settle them, or the only control the user has over a misbehaving panel
    would strand every unit already inside it. That is why `daemon.validation_tick`
    does not check it, and why adding a check there for symmetry is a bug.
    """
    name, path, _wo = find_work_order(wo_id)
    cfg = validation_config(name)
    store = ProjectStore(path)
    try:
        open_requests = store.open_approvals(wo_id)
        if open_requests:
            # WHICH request the message describes is a choice, not a consequence of how
            # `open_approvals` happens to order its two halves: the way on differs by
            # status, and a held one is the only one the worker can act on this turn, so
            # it leads whenever both are open.
            from .gates import AWAITING_CASE

            raise OpsError(gate_still_open(wo_id, next(
                (a for a in open_requests if a["status"] == AWAITING_CASE),
                open_requests[0])))
        # Read ONCE, before anything is written: a refused finish must leave the record
        # exactly as it found it (a `result_summary` saved beside a refusal reads
        # afterwards like a work order that finished), and the abandonment below records
        # what this same read saw rather than re-running git against a tree that has
        # moved.
        stranding = unlanded_work(store, _wo, pr_url or "")
        if stranding.produced and not abandon:
            raise OpsError(unlanded_refusal(wo_id, summary, stranding))
        # `stranding` is the REFUSAL's reading and is deliberately empty for an order
        # carrying a pull request; `base` is what says a worktree was actually read. The
        # record wants the other answer, so the `--pr` path does the look here instead —
        # once either way. See `authorship` and INV-PR-RECORDED.
        work = stranding if stranding.base else authorship(store, _wo)
        fields: dict[str, Any] = {"result_summary": summary}
        if pr_url:
            fields["pr_url"] = pr_url
        store.update_work_order(wo_id, **fields)
        fresh = store.get_work_order(wo_id)
        # BEFORE anything settles, because settling reads it: `refusal_answered` dates
        # the delivery against the user's last refusal, and an event written afterwards
        # would make every re-delivery look older than the refusal it answers.
        # The evidence rides in the payload so the OTHER route into done can find it —
        # `review_work_order` has no `--evidence` of its own. See `declared_evidence`.
        store.add_event(wo_id, "finished",
                        {"summary": summary,
                         **({"pr_url": pr_url} if pr_url else {}),
                         **({"evidence": evidence} if evidence else {}),
                         **({"authored": work.record()} if work.base else {})})
        if abandon:
            # AFTER `finished` and never before: `_abandoned` reads the newer of the two,
            # so an abandonment written first would be superseded by the finish it
            # belongs to and the landing would refuse the very order it just excused.
            store.add_event(wo_id, "abandoned",
                            {"reason": abandon, **work.record()})
            if _wo["status"] in TERMINAL_STATUSES:
                # RECORDING A DECISION ABOUT SETTLED WORK IS NOT A DELIVERY. This is the
                # remedy INV-WORK-LANDED prints, and it is typed against orders that
                # settled weeks ago — so everything below would fire on work nobody is
                # doing: a validation round opens over a session that is gone, the panel
                # answers "this submission changes no files", the order is escalated to
                # `needs_review` and an attention item appears. That happened to
                # `wo-5eedc84d` on 2026-09-18, which is how this was found: the user
                # cleared one alert and got a different one back. The abandonment is
                # written above, `work_abandoned` reads it, the alert is clear; a settled
                # order's status is not this command's to move.
                return {"project": name, "wo_id": wo_id, "status": _wo["status"]}
        opened = bounced = None
        if cfg is not None and cfg.enabled:
            opened = submit_for_validation(store, path, fresh, declared=evidence,
                                           cfg=cfg)
            # None means BOUNCED, and only here: with validation off no submission was
            # attempted at all, and the two must not collapse into one `opened is None`.
            bounced = opened is None
        if str((opened or {}).get("outcome") or "") == "escalated":
            # The bounce ceiling, and the ONE case where a submission has already settled
            # itself: the user has been asked, and the landing below would land the
            # give-up. Spec docs/superpowers/specs/2026-09-22-a-round-must-answer-the-
            # list.md §6.
            status = str(store.get_work_order(wo_id)["status"] or "")
        else:
            # ...and the status is the JOIN's to decide, not this branch's — but a
            # BOUNCE is told to it rather than re-derived from the latest round, which
            # is not the row the bounce read. See `land_when_cleared`'s `panel_open`.
            status = land_when_cleared(store, fresh, pr_url, panel_open=bool(bounced))
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

    **IT RECORDS UNLANDED WORK AND DOES NOT REFUSE IT**, which is the one place this
    parts company with the other two landings (GitHub issue #232). They refuse because
    nobody decided; this IS the decision — a user typing `jarvis wo done` over a pull
    request that will never merge is the documented exit, and refusing it would leave
    them with no way to close the work order at all. What issue #232 actually asks for
    is that the evidence stop being written over in silence, so the event goes on the
    record and the status does not change.

    **"UNLANDED" IS THE WORKTREE *OR* THE PULL REQUEST**, and it used to be only the
    first. `unlanded_work` answers "nothing" for any order carrying a `pr_url`, so this
    wrote no event for exactly the population INV-WORK-LANDED now checks — the remedy the
    user was told to type recorded nothing, and the alert came back an hour later. See
    `unmerged_pull_request`.
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


def void_round_for_settled_pr(store: ProjectStore, wo: dict[str, Any],
                              reason: str) -> int | None:
    """Close an open validation round whose question the pull request just answered.

    Called by the pull-request poll immediately before `complete_merged` or
    `record_pr_closed`, and only by them. A work order can be `validating` while its pull
    request is merged or closed by hand — `rejudge_moved_head` opens a round on a head the
    OS's own repair loop moved, and the user is right there at the pull request — and a
    round judging a diff that has already landed, or been refused, is spent work whatever
    it decides.

    `void` and not `passed`, `rejected` or `failed`: nobody judged this and nothing is
    left undecided, which is precisely what that outcome means (`project_store.
    VALIDATION_OUTCOMES`). It costs the submitter no round, it is in none of the OPEN or
    RUNNABLE sets, and `invariants.validation_escalated` cannot re-derive a give-up from
    it — so the round machine and the reconciler both let the work order go. THE SEATS
    MAY STILL BE READING, and that is the other half: `Daemon._validate_work_order`
    re-reads this outcome when its validator returns and drops the verdict rather than
    writing over a round that settled underneath it.

    DELIBERATELY NOT `Daemon._void`, which ends with `land_when_cleared`: that would park
    the order back in `waiting_pr_merge` a line before the caller completes or refuses it,
    and the last writer would be deciding the status by accident. This one closes the
    round and stops, leaving the ending to the caller that knows it.

    Returns the round number it voided, or None when there was nothing open — the ordinary
    case, and the reason this is safe to call unconditionally.
    """
    rnd = store.latest_validation_round(wo_id=wo["id"])
    if not rnd or not store.round_machine_owns(rnd):
        return None
    store.close_validation_round(rnd["id"], "void", reason)
    store.add_event(wo["id"], "validation_void",
                    {"round": rnd["round"], "round_id": rnd["id"], "reason": reason})
    return int(rnd["round"])


def complete_merged(store: ProjectStore, wo: dict[str, Any],
                    merged_at: str | None = None,
                    head_oid: str = "",
                    automerge: dict[str, Any] | None = None) -> dict[str, Any]:
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

    `head_oid` is the sha that merged, and it goes on the event because THE EVENT IS THE
    ONLY PLACE IT CAN LIVE. `pr_state` is stale by construction with one permitted reader
    (kn-dbc4971d), and asking GitHub again months later is a round trip per settled work
    order; a fact recorded when it was true is neither. It answered issue #232's Mode C
    exactly — commits the branch grew AFTER the merge — until the user narrowed
    INV-WORK-LANDED to the pull request on 2026-09-18, and NOTHING READS IT NOW. It stays
    because a merge whose commit is not written down anywhere is a hole in the record,
    and this is the one path that knows it.

    `automerge` says WHO merged it, which `head_oid` cannot: it is the `pr_merged` rule
    above cutting the other way. A merge the OS performed itself is indistinguishable
    here from one the user performed — the same `gh pr view` reports both — so the ONE
    path that knows the difference says so, in an `automerge_merged` event written before
    the close-out. It carries the approval the merge ran under, the round that accepted
    the diff and the commit that landed, which is the whole audit trail: without it the
    timeline would read as though a person merged this, and the only record that the OS
    holds merge authority at all would be in the gate ledger, one lookup away from the
    work order it acted on. `None` is the ordinary case: a human merged it.
    docs/superpowers/specs/2026-09-14-validated-auto-merge-design.md §8.
    """
    if automerge:
        store.add_event(wo["id"], "automerge_merged",
                        {**automerge, "pr_url": wo.get("pr_url")})
    store.update_work_order(wo["id"], pr_state="MERGED")
    stopped = close_out(store, wo, "pr_merged", why="pull request merged",
                        payload={"pr_url": wo.get("pr_url"), "merged_at": merged_at,
                                 **({"head_oid": head_oid} if head_oid else {})})
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
    # EVERY REPAIR EPISODE ENDS HERE, because there is nothing left to repair: the pull
    # request is shut. Only the open-and-mergeable branch of the poll used to close one,
    # so a red build that spent its three attempts and was then closed unmerged kept
    # saying "do not merge it as it stands" — over the news that nobody is going to.
    # That is a true line hiding a truer one, which is the shape kn-b6977de3 is about and
    # the shape this work order exists to close. It also makes the ranking in
    # `true_blockers` unnecessary rather than merely favourable: a refusal has no
    # give-up left to be outranked by. Reopening starts a fresh budget, which is right —
    # the fix the worker never landed is three attempts away again.
    for repair in PR_REPAIRS:
        clear_pr_repair(store, wo, repair)
    # DERIVED, not asserted. PR_CLOSED_BLOCKER is what this usually is and the fallback
    # keeps that true when nothing is derivable yet — but since issue #224 widened the
    # poll, a work order reaching here may already owe the user an assumption decision,
    # and that line outranks this one. Writing the refusal into the column anyway made
    # INV-ATTENTION-REASON rewrite it on the very next tick, which is the invariant
    # correcting a reason this function had no business choosing.
    fresh = store.get_work_order(wo["id"])
    blockers = true_blockers(store, fresh)
    store.flag_attention(wo["id"], blockers[0] if blockers else PR_CLOSED_BLOCKER)
    return {"wo_id": wo["id"], "status": "needs_review", "was": wo["status"],
            "pr_url": wo.get("pr_url")}


#: Baked into every repair nudge, not a format field, so a new nudge cannot omit it.
#: The dispatch brief states the same rule (kn-356c724b).
TARGETED_TESTS_LINE = ("run only the tests covering the files you touched (NOT the "
                       "full suite — that is CI's job, and it runs it on more "
                       "interpreters than you can)")


#: The nudge the user used to type by hand, written once so it can be complete: what is
#: wrong, what to do, what NOT to do, and how many attempts are left. Spec §3.
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


#: The same message for a red build. Same four things the conflict nudge says — what is
#: wrong, what to do, what NOT to do, how many attempts are left — plus the names of the
#: checks, because "CI is red" without them costs the worker a `gh` call to find out.
#: docs/superpowers/specs/2026-09-13-a-work-order-never-sits-on-a-red-pull-request.md §3.
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

#: Appended to the nudge above when the branch is also BEHIND its base. SAID, NEVER DONE
#: — spec §5 for why the OS reports this rather than running the update itself.
#:
#: THE ONLY PLACE BEHIND IS EVER MENTIONED, and it is a rider rather than a report: the
#: conflict nudge does not carry it (that worker is merging its base in anyway, which
#: cures BEHIND too) and a green branch is nudged about nothing. So a pull request that
#: is green, non-conflicting and merely behind is reported to NOBODY by Jarvis, even
#: though this repository's ruleset will refuse to merge it. Deliberate: GitHub already
#: says so on the merge page with the "Update branch" button beside it, and `main` moves
#: under every open pull request in the fleet — a second telling would be an attention
#: item per movement about something one click fixes. Spec §5 states this in full.
PR_BEHIND_NOTE = """ The branch is also behind `{base}`, which this repository's ruleset \
requires it not to be before a merge; while you are in there, merge `origin/{base}` in \
(do not rebase) so the checks re-run against what would actually land."""


@dataclass(frozen=True)
class PrRepair:
    """One thing a poll can ask a worker to fix on its own pull request.

    Two of them, and everything else is shared: the same attempt cap, the same three
    guards in `Daemon.heal_pull_request`, the same episode arithmetic, the same
    unauthored message source. They differ only in what is wrong and what to say about
    it, which is the whole reason this is a descriptor and not a second copy of the
    functions below (issue #224: the CI half was missing because it looked like new
    machinery).
    """

    #: Names the event kinds (`pr_<name>_nudged`/`_cleared`/`_unresolved`), the message
    #: source (`pr-<name>`, which `timeline.UNAUTHORED_SOURCES` renders as Jarvis rather
    #: than as the user) and the episode `ProjectStore.pr_repair_attempts` counts.
    #: ALWAYS one of `invariants.PR_*_REPAIR`, never a literal written here — see the
    #: note beside those constants for why the name cannot live in this module.
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

#: Newest episode first is not a thing here — `pr_repair_origin` compares timestamps —
#: but every repair that can hold a work order out of its status has to be in this
#: tuple, or `Daemon.settle_work_order` will park its repair turn in the merge queue.
PR_REPAIRS = (PR_CONFLICT, PR_CHECKS)


def nudge_pr_repair(store: ProjectStore, wo: dict[str, Any], repair: PrRepair,
                    **fields: Any) -> dict[str, Any]:
    """GitHub says this pull request is broken: ask the worker to fix it, or give up.

    Queues the message and records the attempt — delivery, the resume and the return to
    wherever the work order came from are all existing machinery, and nothing here runs
    git (spec §3). Past PR_REPAIR_MAX_ATTEMPTS it stops and flags the user instead, once
    (spec §4).

    `was` is the status the repair is taking the work order OUT of, recorded so that
    settlement can put it back: a `needs_review` order nudged about a red build still
    needs review, and parking it in the merge queue would take the item off the user's
    list (Neo question 275, red-PR spec §4).
    """
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
        # Not "jarvis" and not "user": this message had no author, a poll wrote it, and
        # the timeline says so (spec §6).
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
    """Say once that a pending gate is why this repair is not being attempted.

    Issue #469, §3 of
    docs/superpowers/specs/2026-09-19-an-attempt-the-worker-could-not-make.md. No
    attempt is spent and no message is queued; this is only the record.

    ONCE PER EPISODE PER REQUEST, because the poll asks every couple of minutes for as
    long as the review takes — six hours of it, in the issue. The attention the gate
    deserves is the gate's own, which is already an item when it escalates.
    """
    if store.pr_repair_deferred(wo["id"], repair.name, request["id"]):
        return False
    store.add_event(wo["id"], repair.event("deferred"), {
        "pr_url": wo.get("pr_url"), "approval_id": request["id"],
        "kind": request["kind"],
        "attempts": store.pr_repair_attempts(wo["id"], repair.name)})
    return True


def rearm_pr_repair(store: ProjectStore, wo: dict[str, Any],
                    repair: PrRepair) -> int:
    """Give back a budget spent on turns the worker was never allowed to take.

    Issue #469, §4 of the spec above. Returns the number of attempts refunded, 0 when
    there is nothing to refund — which is every ordinary give-up, and the answer this
    must keep giving for a worker that genuinely tried and failed.

    THE THREE CONDITIONS ARE IN THE CHEAPEST ORDER: an episode that has not given up is
    the overwhelmingly common one and costs a single indexed read to rule out. The last
    is the real predicate — EVERY nudge went out while a gate was open, so not one of
    them could reach the conflict. Partial refunds are deliberately not a thing; see the
    spec for why the whole-budget case is the one worth machinery.

    The caller nudges immediately afterwards, which is what keeps `pr_repair_origin`
    answering: the fresh attempt re-records the status the repair is taking the work
    order out of, and the episode is never left open with no nudge in it.

    ONCE PER WORK ORDER PER REPAIR, EVER, and that cap is not derived from the other
    three conditions — it is the thing that makes a runaway refund impossible to write.
    The argument that the conditions alone terminate is sound but it is an argument
    about two predicates in two files agreeing; they disagreed once already (round 1 of
    this order's review), and the failure mode is invisible: a budget silently restored
    every episode, attention cleared each time, exactly the unattended burn issue #469
    is about. The cost of the cap being wrong is one work order asking the user to
    resolve a conflict by hand, which is where the OS started. Spec §4.
    """
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
    """The pull request is well again: close the episode. True if there was one.

    Resets the attempt budget (spec §4) and takes down the give-up flag — but only that
    one, never a flag raised for something else. That last clause is what keeps a green
    build from clearing the review the user still owes.

    ...and then RE-DERIVES the status, because closing the episode is the moment the
    origin snapshot `settle_work_order` replayed becomes garbage — issue #705 defect 1.
    """
    if not store.pr_repair_attempts(wo["id"], repair.name):
        return False
    store.add_event(wo["id"], repair.event("cleared"), {"pr_url": wo.get("pr_url")})
    if wo["attention_reason"] == repair.blocker:
        store.clear_attention(wo["id"])
    resettle_after_repair(store, wo["id"])
    return True


def repaired_since_finish(store: ProjectStore, wo_id: str) -> bool:
    """Did a repair episode open AND close after this work order delivered?

    The shape issue #705 describes and the bound on `resettle_after_repair`: without it
    that function would re-derive the status of every `needs_review` order holding a
    pull request, which is far wider than the defect and would move orders no repair ever
    touched.
    """
    finished = store.events_of_kind(wo_id, "finished")
    if not finished:
        return False
    since = float(finished[-1]["ts"])
    return any(float(e["ts"]) > since
               for repair in PR_REPAIRS
               for e in store.events_of_kind(wo_id, repair.event("cleared")))


def resettle_after_repair(store: ProjectStore, wo_id: str) -> bool:
    """Put a repaired work order back in the merge queue if nothing else holds it.

    Issue #705 defect 1. `nudge_pr_repair` records the status the repair took the work
    order out of and `Daemon.settle_work_order` restores it (Neo question 275) — but
    that snapshot is taken when the nudge goes out, and the whole point of a repair is
    that time passes. A `needs_review` order nudged about a conflict, whose assumptions
    the user accepts mid-repair, was put back into `needs_review` for a reason that had
    already gone, and `waiting_pr_merge` is the only status the auto-merge poll looks
    at: the order left the merge queue for ever with a green, mergeable pull request.

    ONLY OUT OF `needs_review`, and only when the whole of that status's triage in
    `true_blockers` says nobody owes anything: `failed` and `waiting_input` mean
    something the repair never addressed. Every blocker of the row it WOULD become is
    derived first and any one of them refuses the move, so the flag that comes down
    afterwards is always empty by construction.
    """
    wo = store.get_work_order(wo_id)
    if (wo["status"] != "needs_review" or not wo.get("result_summary")
            or not _awaiting_merge(wo)):
        return False
    if not repaired_since_finish(store, wo_id):
        return False
    # THE REPAIR MUST BE WHY IT IS HERE NOW, not merely the last thing that closed.
    # `repaired_since_finish` says an episode opened and closed after the finish; it says
    # nothing about the turns AFTER it. A user-opened turn that ends without `jarvis wo
    # finish` puts the order into `needs_review` on its own account, and this function
    # lifts a merge hold, so without this every later hold fails open into an unattended
    # merge.
    if store.turn_opened_by(store.latest_turn(wo_id)) not in invariants.PR_REPAIR_SOURCES:
        return False
    # The other repair's episode may still be open, and it owns the status while it is.
    if pr_repair_origin(store, wo_id) or store.queued_messages(wo_id):
        return False
    # The `needs_review` triage in `true_blockers` — the probe below cannot see these,
    # because they are derived under the status this function is trying to leave.
    if (store.pending_assumptions(wo_id)
            or invariants.validation_escalated(store, wo)
            or store.work_unlanded_open(wo_id)):
        return False
    # JUDGE THE ROW IT WOULD BECOME, BEFORE WRITING ANYTHING. An escalated gate is a
    # blocker at any status, so re-deriving the flag after `set_status` would leave a
    # flagged order already back in the merge queue — the poll acts on the status, not
    # on the flag. Probed through the one function that owns blockers rather than
    # re-listing them here (kn-78346a2d).
    probe = dict(wo)
    probe["status"] = "waiting_pr_merge"
    if true_blockers(store, probe):
        return False
    store.set_status(wo_id, "waiting_pr_merge")
    store.add_event(wo_id, "pr_repair_resettled",
                    {"pr_url": wo.get("pr_url"), "was": wo["status"]})
    store.clear_attention(wo_id)
    return True


#: The event `carry_validated_head` writes. Not a `PrRepair` and not an automerge event:
#: it is a fact about a VERDICT, and it belongs beside the round it moved.
HEAD_CARRIED_EVENT = "validation_head_carried"


def record_base_health(store: ProjectStore, wo: dict[str, Any], *, red: bool,
                       base: str) -> bool:
    """Write down whether this pull request's base is broken. ONLY ON A CHANGE.

    True when a row was written. The transition discipline is not tidiness: it is what
    lets `invariants.base_red_note` answer the status line in one indexed read (see it),
    and it keeps the timeline readable — a work order parked for a day across a red base
    would otherwise carry seven hundred identical rows saying nothing happened.
    """
    from . import db, invariants as invariants_mod

    rows = store.events_of_kind(wo["id"], invariants_mod.PR_BASE_HEALTH_EVENT)
    if rows:
        last = db.from_json(rows[-1].get("payload"), {}) or {}
        if bool(last.get("red")) == red:
            return False
    elif not red:
        # NOTHING TO SAY. A green base is the ordinary state of the world, and a first
        # row asserting it would put a line on the timeline of every parked work order
        # in the fleet the first time its pull request went red for its own reasons.
        return False
    store.add_event(wo["id"], invariants_mod.PR_BASE_HEALTH_EVENT,
                    {"red": red, "base": base, "pr_url": wo.get("pr_url")})
    return True


def base_heal_spent(store: ProjectStore, wo_id: str, base_sha: str) -> bool:
    """Has this pull request already been rebuilt against THIS base commit?

    The bound, and it is keyed on the base sha rather than counted: one update per
    (pull request, base sha). If the merge ref was rebuilt on a green base and CI is
    still red, the failure is the branch's own and the worker nudge is the correct next
    step — retrying the update would only produce the same commit. A LATER base recovery
    is a different sha and earns a fresh attempt, because it is a fresh question.

    A REFUSED update counts as spent, the same as a successful one. Otherwise a pull
    request GitHub will not update — already up to date, or a conflict it will not touch
    — is retried every two minutes for ever and never reaches the worker who could
    actually fix it.
    """
    from . import db, invariants as invariants_mod

    for kind in (invariants_mod.PR_BASE_UPDATED_EVENT,
                 invariants_mod.PR_BASE_UPDATE_FAILED_EVENT):
        for row in store.events_of_kind(wo_id, kind):
            if (db.from_json(row.get("payload"), {}) or {}).get("base_sha") == base_sha:
                return True
    return False


def record_base_update(store: ProjectStore, wo: dict[str, Any], *, base: str,
                       base_sha: str, head_before: str, head_after: str,
                       checks: tuple[str, ...]) -> None:
    """The OS healed this pull request itself. Spec §4: say so, on the record.

    `head_before`/`head_after` are not decoration — they are what `carry_validated_head`
    is allowed to rely on afterwards, and what tells a reader six weeks later that the
    commit an automatic merge landed differed from the judged one by a base merge and
    nothing else.
    """
    from . import invariants as invariants_mod

    store.add_event(wo["id"], invariants_mod.PR_BASE_UPDATED_EVENT,
                    {"pr_url": wo.get("pr_url"), "base": base, "base_sha": base_sha,
                     "head_before": head_before, "head_after": head_after,
                     "checks": list(checks)})


def record_base_update_failed(store: ProjectStore, wo: dict[str, Any], *, base: str,
                              base_sha: str, reason: str) -> None:
    """GitHub refused the update. Spends the attempt — see `base_heal_spent`.

    `reason` is `GitHubError.reason`, this OS's own short phrase, never `str(e)`: that
    carries `gh`'s stderr, and the vocabulary note on `github.GitHubError` applies
    wherever remote text would end up on a surface a person reads.
    """
    from . import invariants as invariants_mod

    store.add_event(wo["id"], invariants_mod.PR_BASE_UPDATE_FAILED_EVENT,
                    {"pr_url": wo.get("pr_url"), "base": base, "base_sha": base_sha,
                     "reason": reason})


def carry_validated_head(store: ProjectStore, wo: dict[str, Any], *, judged: str,
                         head_after: str, base: str, base_sha: str,
                         parents: tuple[str, ...]) -> dict | None:
    """Bind the panel's existing verdict to the commit the OS's own merge produced.

    **THE HEAL IS NOT DONE WHEN CI GOES GREEN; IT IS DONE WHEN THE PULL REQUEST CAN
    MERGE.** Updating the branch moves the head, and `automerge.decide` refuses a commit
    no round judged — so without this the heal turns a red pull request into a green one
    held on `sha_moved`, which is the same stall with a nicer label. Both production
    pull requests did exactly that on 2026-09-18 when the user updated them by hand, and
    both needed `jarvis validation force` to recover.

    **THE THREE FACTS, and the carry is refused unless all hold** (spec §5):

    1. the latest round PASSED and the commit it accepted was `judged` — so there is a
       verdict, and it is the one this heal started from;
    2. `judged` was the head the OS updated FROM — the caller re-reads it AFTER its
       guards and immediately before the update, so a worker push that beat us means
       `judged != head_before` and nothing is carried;
    3. **`head_after` IS A MERGE COMMIT WHOSE FIRST PARENT IS `judged`** — proved by
       reading the commit back from GitHub (`ci.commit_parents`), not inferred from
       having asked for the update.

    Together they say the difference between the judged commit and the new one is a
    merge of the base and nothing else: `ci.update_branch` merges (never rebases), and
    GitHub refuses the update outright on conflict rather than letting anyone resolve
    one, so no authored content can enter this way. Any later push moves the head off
    `head_after` and `decide` holds `sha_moved` again — correctly, because then there IS
    new authored content.

    **FACT 3 IS NOT BELT AND BRACES; IT IS THE ONLY ONE THAT CANNOT BE RACED** (review
    round 1). `gh pr merge` can pin its commit server-side with `--match-head-commit`;
    `gh pr update-branch` has no such flag, so between reading the head and the update
    landing there is a window in which a worker turn can end and push. Fact 2 narrows
    that window and cannot close it. Fact 3 closes it, because it asks GitHub what the
    resulting commit ACTUALLY merged: a head built on a worker's push has that push as
    its first parent, not `judged`, and the carry is refused. The failure this guards
    against is silent and falls open — it would bind the panel's verdict to code the
    seats never read and record "no authored content changed" beside it, a false
    justification that `automerge.decide` and the gate request would both then repeat
    to Neo.

    Exactly two parents, first one `judged`. Two because a rebuilt merge ref has the old
    head and the base and nothing else; FIRST because that is the side the merge was
    made ONTO, and a commit merging `judged` in as its second parent is a different
    history with someone else's work at its root.

    What is NOT relaxed: CI must still pass on the carried commit (condition 6), and the
    merge still files its AUTO_MERGE gate for Neo. This changes what that request says,
    never whether one happens.
    """
    row = store.latest_validation_round(wo_id=wo["id"])
    if row is None or not judged or not head_after or head_after == judged:
        return None
    if ProjectStore.validated_head(row) != judged:
        return None
    if len(parents) != 2 or parents[0] != judged:
        log.info("%s: not carrying round %s onto %s — its parents are %s, not a merge "
                 "of %s with the base", wo["id"], row["round"], head_after[:10],
                 [p[:10] for p in parents] or "unreadable", judged[:10])
        return None
    reason = (f"the OS merged `{base}` ({base_sha[:10]}) into this branch to clear a "
              f"failure inherited from a red base — no authored content changed")
    store.carry_round_head(int(row["id"]), head_after, reason)
    carried = {"round": int(row["round"]), "round_id": int(row["id"]),
               "judged_sha": judged, "carried_head_sha": head_after,
               "base": base, "base_sha": base_sha, "reason": reason,
               # THE PROOF, not a restatement of the claim: the commit's own parents as
               # GitHub answered them. `merged_base_sha` is the base commit that
               # actually went in, which is not necessarily `base_sha` — the base can
               # move between the CI read and the update.
               "parents": list(parents), "merged_base_sha": parents[1]}
    store.add_event(wo["id"], HEAD_CARRIED_EVENT, carried)
    return carried


def pr_repair_origin(store: ProjectStore, wo_id: str) -> str | None:
    """The status an OPEN repair episode took this work order out of, if any."""
    return store.pr_repair_origin(wo_id, tuple(r.name for r in PR_REPAIRS))


#: What a relaunched turn may put a work order back into, and the only status that needs
#: it. `needs_review` is a decision the USER owes — pending assumptions are handled a
#: branch earlier, so what is left is a red build, a refused panel round, a pull request
#: closed unmerged — and a usage window reopening answers none of them; parking that in
#: the merge queue is the silent downgrade Neo question 275 already outlawed for repair
#: turns. Every other origin is deliberately absent: `waiting_pr_merge` is where the
#: fallback lands anyway, `failed` and `waiting_input` are the states the relaunch was
#: meant to LIFT, and returning a work order to one would undo its own recovery.
RESUMABLE_ORIGINS = ("needs_review",)


def resumed_from(store: ProjectStore, wo_id: str, seq: int) -> str | None:
    """The status `Daemon.retry_paused_turns` took this work order out of for THIS turn.

    The other way the OS moves a work order without being asked, and the counterpart to
    `pr_repair_origin` above (issue #259). Not episode arithmetic: a resume is one turn,
    so the origin is spent by the turn that settles and matching on `seq` says so
    exactly. A `turn_resumed` event from an earlier turn describes a move the OS has
    already finished with, and reading it here would park a work order in a status it
    left two turns ago.
    """
    events = store.events_of_kind(wo_id, "turn_resumed")
    if not events:
        return None
    payload = db.from_json(events[-1].get("payload"), {}) or {}
    if payload.get("seq") != seq:
        return None
    was = payload.get("was")
    return was if was in RESUMABLE_ORIGINS else None


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


def ack_os_flag(wo_id: str, project_name: str | None = None) -> dict[str, Any]:
    """Put down an attention flag the OS raised for ITSELF, acknowledging nothing.

    `ack_attention` above is the user saying "I have seen this": it writes every live
    blocker into `acknowledged_blockers`, and `true_blockers` then filters them out for
    ever. The supervisor, the remedy applier and Neo's alarm answer were all calling it
    to take down the flag THEY had raised (`supervisor.ALARM_BLOCKER`), and so dismissed
    blockers the user had never been shown — issue 573, where an alarm ack silently
    buried an IDLE_NO_FINISH the user never saw, permanently.

    THERE IS NOTHING NARROW TO ACK, which is why this writes no acknowledgement at all:
    `ALARM_BLOCKER` is not a string `true_blockers` derives (the alarm row is the memory
    — see `supervisor._apply`), so there is no entry to add. Instead the blockers are
    re-derived and the flag either goes down, because nothing is left, or is re-raised
    against whatever IS left — which is how the blocker the alarm was masking comes back
    with its own reason rather than disappearing.

    A pending assumption needs no guard here, unlike in `ack_attention`: it is a blocker
    like any other, so it simply re-flags.
    """
    from .invariants import true_blockers

    name, path, _ = find_work_order(wo_id, project_name)
    store = ProjectStore(path)
    try:
        wo = store.get_work_order(wo_id)
        blockers = true_blockers(store, wo)
        if blockers:
            # Only when it would actually change: `flag_attention` writes a timeline row
            # every call, and re-stating the same reason is noise on the record.
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


def _validates_on_review(store: ProjectStore, wo_id: str, cfg: Any) -> bool:
    """Should accepting this work order's assumptions open a validation round?

    Switched on, and never judged. Anything with a round on record has been through the
    loop already, so an acceptance is the user's decision on top of the machine's rather
    than an input to it.

    NOW A CATCH-UP FOR THE PAST ONLY, and kept for exactly that. `finish` opens the round
    itself whether or not assumptions are pending (spec
    docs/superpowers/specs/2026-09-13-two-gates-not-a-chain.md §1), so every work order
    finished from that release on already has one by the time it reaches here. The ones
    parked in `needs_review` when it shipped do not, and deleting this would send them to
    the merge queue unjudged.
    """
    return (cfg is not None and cfg.enabled
            and store.latest_validation_round(wo_id=wo_id) is None)


def _land_after_acceptance(store: ProjectStore, path: Path, wo_id: str,
                           cfg: Any) -> str:
    """Where a work order goes once its assumptions are all accepted. Returns the status.

    ONE function for both routes into that state — the user's `jarvis wo review` and the
    OS's `accept_assumption` — because the OS accepting an assumption must be
    indistinguishable IN EFFECT from the user accepting it. Two copies of this would be
    two answers to "does a closed pull request go back into the merge queue", and the one
    that is not the route actually running is the copy that rots (`arbitrate`'s lesson,
    and `land_finished`'s own note about its two callers).
    """
    if _validates_on_review(store, wo_id, cfg):
        submit_for_validation(store, path, store.get_work_order(wo_id),
                              declared=declared_evidence(store, wo_id), cfg=cfg)
    fresh = store.get_work_order(wo_id)
    # THE ONE PLACE `pr_state` MAY BE READ, and the reason is that this is the only
    # landing the poll can have run before: a work order reaches here parked, and
    # `_awaiting_merge` is asking about the pull request that parking was about. A
    # settled one is not a merge queue — passing it out of the landing is what stops a
    # review putting a CLOSED pull request back in front of a poll whose only move is to
    # flag it for the user again. Everywhere else the column can be stale; `land_finished`
    # says why.
    if not _awaiting_merge(fresh):
        fresh = {**fresh, "pr_url": ""}
    # The assumption gate has cleared; whether the work order lands now is the panel's
    # half of the join to answer. NOTE that landing through `land_finished` also CLOSES
    # THE BACKLOG ITEM on the `completed` branch, which the inline landing this replaced
    # did not — that omission was the drift `land_finished` exists to prevent.
    return land_when_cleared(store, fresh)


def accept_assumption(store: ProjectStore, project_path: Path, wo: dict[str, Any],
                      assumption: dict[str, Any], *, reason: str, model: str,
                      question_id: int, cfg: Any,
                      config_version_id: str | None = None) -> dict[str, Any]:
    """THE OS settling ONE assumption on the user's behalf. Never the user's route.

    docs/superpowers/specs/2026-09-15-neo-decides-an-assumption.md §4. Takes an open
    store, `complete_merged`'s way, because the only caller is the daemon thread that
    already has one.

    **EVERY FACT THAT MAKES THIS AUDITABLE IS WRITTEN HERE, and that is the single most
    important post-condition of the feature**: who decided (never the user), the reason,
    the model that reached it and the configuration in force. The row carries them so
    that every surface listing assumptions inherits the attribution without each having
    to remember; the event carries them so the timeline reads as one fact.

    **NO LEARNING IS RECORDED.** `review_work_order` distils the USER's reasoning into a
    Neo learning, which is the point of asking them for it. Doing the same here would be
    Neo teaching itself out of its own output, and a ledger that cites itself is how a
    single early mistake becomes a standing rule. The teachable half is the other
    direction and it already exists: `jarvis neo review <qid> --correct "…"` on the
    question this settled, and `jarvis neo retract` on anything it produced.

    Lands the work order only when this was the LAST pending assumption — through the
    same `_land_after_acceptance` the user's route uses, so the two cannot drift.
    """
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


#: The one transport that ships. `peer` is in `OBJECTION_TRANSPORTS` and unused: §3's
#: spike could not verify the sender's identity, and §6.4 says the peer path does not ship
#: without it (Neo question 583).
OBJECTION_TRANSPORT = "queue"


def file_assumption_objection(store: ProjectStore, project_path: Path,
                              wo: dict[str, Any], assumption: dict[str, Any], *,
                              reason: str, model: str,
                              question_id: int | None) -> dict[str, Any]:
    """THE OS telling a RUNNING worker it disagrees with an assumption it just recorded.

    docs/superpowers/specs/2026-09-23-an-assumption-judged-while-the-worker-still-runs.md
    §6.1. Takes an open store, `accept_assumption`'s way, because the only caller is the
    daemon thread that already has one.

    **THE ORDER IS THE POINT AND IT IS NOT NEGOTIABLE.** The envelope, the assumption row
    and the timeline event are all written before anything can reach a wire: an objection
    that exists only on the wire is one the record cannot explain, and a send that
    succeeds after a crash leaves a worker acting on guidance nothing accounts for. The
    message a worker reads does not exist yet when this returns — `bus.post` queues an
    envelope and `Daemon.deliver_envelopes` turns it into a `wo_messages` row on a later
    tick, which is why the row stores an ENVELOPE id.

    **This is not an acceptance and not a rejection.** `assumptions.status` is untouched,
    nothing here reaches `ops.accept_assumption`, and the settlement verdict space is
    still ACCEPT or ESCALATE (§2 of both specs). It is guidance, and the user still owes
    the decision.

    Two side effects of the neighbouring send paths are deliberately NOT inherited (§6.3):
    the message is unattributed (`authored_by=''`, because only the user's own words are
    ever stamped) and the work order's attention is left exactly as it was.
    """
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
    """THE OS's verdict on an assumption while the worker is still typing. Settles nothing.

    docs/superpowers/specs/2026-09-23-an-assumption-judged-while-the-worker-still-runs.md
    §5.3. Beside `accept_assumption` and deliberately NOT inside it: that one stamps
    `decided_by`, writes `autoreview_accepted` and lands the work order behind it, and
    every one of those three is wrong for a ruling on an intention. This writes the
    `provisional_*` columns and one event, and `status` stays `pending` — so to
    `pending_assumptions`, `invariants.true_blockers`, `automerge.decide` and the user's
    own review, nothing has changed.

    **BOTH VERDICTS LAND HERE, INCLUDING `object`, AND NOTHING IS SENT.** Recording an
    objection and sending it are two acts in that order (§6.1): the send is
    `file_assumption_objection`'s, driven off the column this writes, so the record cannot
    be behind the wire.
    """
    store.record_provisional(
        assumption["id"], verdict=verdict, reason=reason, model=model, stakes=stakes,
        config_version=(config_version_id if config_version_id is not None
                        else current_config_version()))
    store.add_event(wo["id"], "autoreview_provisional", {
        "assumption_id": assumption["id"], "n": assumption.get("n"),
        "verdict": verdict, "reason": reason, "model": model, "stakes": stakes,
        "neo_question_id": question_id, "decided_by": ASSUMPTION_DECIDER_OS})


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

    **This is the SECOND route into done, and it must validate too.** It is no longer
    the route that OPENS the round — `finish` does that for every work order now, in
    parallel with this review — so what this owes a work order it accepts is the JOIN:
    `land_when_cleared` lands it only if the panel has also finished with it, and leaves
    it `validating` if a round is still in flight. `_validates_on_review` stays for the
    work orders that were already parked here when that shipped.
    """
    name, path, wo = find_work_order(wo_id)
    cfg = validation_config(name)
    store = ProjectStore(path)
    try:
        pending = store.pending_assumptions(wo_id)
        for a in pending:
            # STAMPED `user` RATHER THAN LEFT EMPTY. Empty already reads as the user on
            # every historical row, but this is the route a person takes and the claim is
            # worth asserting: it is what makes "not the user" provable on the other one.
            store.review_assumption(a["id"], "accepted" if accept else "rejected",
                                    decided_by=ASSUMPTION_DECIDER_USER, reason=feedback)
        status = wo["status"]
        if wo["status"] == "needs_review":
            if accept:
                status = _land_after_acceptance(store, path, wo_id, cfg)
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
                         max_parallel: int | None = None,
                         budget_usd: float | None = None,
                         metadata: dict[str, Any] | None = None) -> dict[str, Any]:
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
        return store.create_feature_order(
            title=title, description=description, origin=origin, backlog_id=backlog_id,
            max_parallel=max_parallel, metadata=metadata,
            # A FAMILY budget, so it takes the family default and never the
            # per-work-order one — `catalog.DEFAULT_FEATURE_BUDGET_USD` says why they are
            # two settings rather than one scaled from the other.
            budget_usd=(budget_usd if budget_usd is not None
                        else budget.feature_default_for(_spec_or_none(project_name))))
    finally:
        store.close()


def _spec_or_none(project_name: str) -> Any:
    """This project's catalog entry, or None when the catalog cannot be read.

    None is a DEFAULT, not a failure: the only thing read off the spec here is a standing
    budget, and a catalog that has moved must not stop a work order being filed. No
    catalog means no default means no ceiling — the behaviour the OS has always had.
    """
    try:
        return project_spec(resolve_catalog(), project_name)
    except (OpsError, CatalogError):
        return None


def feature_order_budget(fo_id: str, project_name: str | None = None) -> dict[str, Any]:
    """A feature's family budget, what the family has spent, and what is unreserved."""
    name, path, fo = find_feature_order(fo_id, project_name)
    store = ProjectStore(path)
    central = CentralStore()
    try:
        p = budget.pool(store, central, fo)
        spend = budget.feature_spent(store, central, fo)
        live = budget.feature_in_flight(store, fo)  # display only; see `budget.spent`
        children = [
            {"wo_id": c["id"], "status": c["status"],
             "reserved_usd": c.get("budget_reserved_usd"),
             "spent_usd": budget.spent(store, central, c["id"]).total_usd}
            for c in store.feature_children(fo_id)
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
            for fo in store.list_feature_orders(statuses=statuses, kind=kind):
                out.append({"project": name, **fo,
                            "status_label": feature_status_label(kind, fo["status"]),
                            "progress": feature_progress(store, fo)})
        finally:
            store.close()
    return out


# -- improvement orders ---------------------------------------------------------------
#
# Section 2 of docs/superpowers/specs/2026-09-23-improvement-orders.md. An improvement
# order is a `feature_orders` row with `kind='improvement'`, so everything here that can
# be the feature-order function IS the feature-order function, kind-guarded.

#: `feature_orders.metadata` key: the `--ref` strings exactly as the user typed them.
#: Unresolved and unvalidated on purpose — a reference that does not resolve is a FINDING
#: about the OS's records, not a CLI error (§2.6).
EVIDENCE_REFS_KEY = "evidence_refs"

#: `work_orders.metadata`/`feature_orders.metadata` key on an order FILED FROM a finding:
#: the improvement order it came from. The back-link's machine-readable half — the human
#: half is the first line of the description (§5.3.1). A filed order is never a child:
#: `work_orders.parent_id` references `feature_orders(id)`, so a `type='feature'` proposal
#: has no parent slot at all (§5.3).
ORIGIN_IO_KEY = "origin_io"


def _require_kind(fo: dict[str, Any], kind: str, verb: str) -> None:
    """Refuse a row of the wrong kind, naming the command that WOULD work.

    §2.4 of the improvement-orders spec: the two surfaces share a table, so every verb
    that means something different for the other kind has to say so rather than act.
    """
    actual = fo.get("kind") or "feature"
    if actual == kind:
        return
    other = "an improvement order" if actual == "improvement" else "a feature order"
    raise OpsError(f"{fo['id']} is {other}, not {'an' if kind[0] == 'i' else 'a'} "
                   f"{kind} order — use `{verb} {fo['id']}` instead")


def create_improvement_order(project_name: str, title: str, description: str = "",
                             refs: Sequence[str] = (),
                             budget_usd: float | None = None,
                             origin: str = "jarvis") -> dict[str, Any]:
    """File an observation for an analyst to investigate. Nothing runs here.

    Same shape as `create_feature_order`, down to the budget default, because the whole
    point of the `jarvis io` surface is that a user who knows `jarvis fo` already knows
    it (§2.6). One refusal it adds: no evidence.
    """
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
        return store.create_feature_order(
            title=title, description=description, origin=origin,
            kind="improvement",
            metadata={EVIDENCE_REFS_KEY: refs},
            # The family here is the order plus its analyst, and the family arithmetic is
            # already correct with no children — §2.6, which is why this is the FEATURE
            # default and not the per-work-order one.
            budget_usd=(budget_usd if budget_usd is not None
                        else budget.feature_default_for(_spec_or_none(project_name))))
    finally:
        store.close()


def list_improvement_orders(project_name: str | None = None,
                            include_settled: bool = False) -> list[dict[str, Any]]:
    """`jarvis io list`. The feature-order listing with the other kind asked for —
    behaviour is identical, so a copy would only be a second thing to keep in step."""
    return list_feature_orders(project_name, include_settled=include_settled,
                               kind="improvement")


def show_improvement_order(io_id: str, project_name: str | None = None) -> dict[str, Any]:
    """`jarvis io show` — COUNTS FIRST, then the observation and the evidence.

    Not `show_feature_order` with a flag: that renders a plan, a child tree and a
    progress label, and an improvement order has none of those. The per-finding blocks
    are section 4.4's renderer and deliberately absent here.
    """
    name, path, fo = find_feature_order(io_id, project_name)
    _require_kind(fo, "improvement", "jarvis fo show")
    # `plan` is NULL until the analyst reports, which is the NORMAL state in this
    # section — so every read of it is defensive rather than trusting.
    report = db.from_json(fo.get("plan"), None) or {}
    findings = report.get("findings") or [] if isinstance(report, dict) else []
    by_decision = {"accepted": 0, "rejected": 0, "pending": 0}
    for f in findings:
        status = (f or {}).get("status") or "pending" if isinstance(f, dict) else "pending"
        by_decision[status if status in by_decision else "pending"] += 1
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
        "report": report if isinstance(report, dict) else {},
        "filed_orders": _filed_orders(findings),
        "observation": fo["description"],
        "evidence_refs": list(metadata.get(EVIDENCE_REFS_KEY) or []),
        "analyst": analyst,
        "status_label": feature_status_label("improvement", fo["status"]),
        "alarms": alarms,
    }


def _filed_orders(findings: list[Any]) -> dict[str, list[dict[str, Any]]]:
    """Every order each accepted finding filed, with its CURRENT status (§5.3.1).

    Read LIVE from the stores rather than from the snapshot taken at filing, because the
    question the list answers is which of the proposed fixes are done, running or still
    pending. A fan-out across project stores: a proposal's fix is frequently in another
    project, which is the whole reason a filed order is not a child (§5.3).

    An id that no longer resolves renders as `deleted` rather than vanishing — a link
    that quietly disappears reads as a filing that never happened.
    """
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
    """`jarvis io cancel`. The feature-order path unchanged: it stops every non-terminal
    work order the row owns — here just the analyst — and settles it. Orders already
    filed from accepted findings are independent by construction and are not touched."""
    _, _, fo = find_feature_order(io_id, project_name)
    _require_kind(fo, "improvement", "jarvis fo cancel")
    return cancel_feature_order(io_id, project_name)


def submit_findings(io_id: str, doc: Any,
                    project_name: str | None = None) -> dict[str, Any]:
    """(Analysts) hand back the findings report. The analyst's terminal action.

    Modelled step for step on `submit_plan`, and THE ORDER OF THE STEPS IS THE DESIGN
    (§4.1). The report is validated first, so a bad one costs the analyst one revision
    and nothing else — nothing stored, no user attention spent, no state to unwind. Then
    it is stored and the order is parked for review. Only then is the analyst's work
    order settled: `jarvis io report` IS its `jarvis wo finish`, which is why the analyst
    briefing tells it not to call the latter.

    That last step is CONDITIONAL, which is the one place this parts company with
    `submit_plan`. A resubmission arrives while the order is `plan_review`, by which time
    the analyst has already been settled once, and settling an already-settled work order
    is not an idempotent no-op in this codebase. So a second submission leaves the
    settled analyst alone and omits `analyst` from the return value.
    """
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

    store = ProjectStore(path)
    try:
        # The WHOLE document, replacing whatever was there: a resubmission discards every
        # decision already recorded on the old findings. The status refusal above is what
        # keeps that safe — nothing can be resubmitted once the order has left
        # `plan_review`, which it does as soon as the last finding is decided.
        store.update_feature_order(io_id, plan=db.to_json(report))
        store.set_feature_status(io_id, "plan_review")
        # EXACTLY ONE attention item, raised HERE, at the transition, and nothing
        # re-derives it on a tick (§6.2 and kn-089de524: a flag written on a re-deriving
        # path re-raises itself and overwrites the user's ack).
        store.flag_feature_attention(io_id, findings.review_headline(fo, report))
        analyst_open = False
        if fo.get("plan_wo_id"):
            store.add_event(fo["plan_wo_id"], "findings_submitted", {
                "improvement_order": io_id, "findings": len(report["findings"]),
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
        "note": "queued for the user's decision — end your turn. If it is sent back, the "
                "reason arrives as your next user turn and you revise from this session.",
    }
    if analyst_open:
        # Only while it is still open. A resubmission lands here with the analyst already
        # settled, and settling a settled work order is not an idempotent no-op.
        out["analyst"] = finish(
            fo["plan_wo_id"],
            f"submitted findings for {io_id}: {len(report['findings'])} findings",
        )
    return out


def review_findings(io_id: str, accept: Sequence[str] = (),
                    reject: dict[str, str] | None = None, feedback: str = "",
                    decided_by: str = "user", accept_all: bool = False,
                    project_name: str | None = None) -> dict[str, Any]:
    """Decide a findings report, finding by finding. `jarvis io review`.

    A SEPARATE function from `review_plan` and not an extension of it (§5.1): that one
    takes a single `accept: bool` for a whole plan, this one is per finding, and forcing
    both through one signature re-opens exactly the "two chances to disagree about what
    releasing means" risk that made `review_plan` one function.

    The failure policy is kn-652456b8's durable/best-effort split, with one correction.
    The DECISION and the knowledge write are durable; a proposed order that cannot be
    filed records the error on the finding and leaves the decision standing. But a filing
    failure is NEVER SILENT: it keeps the attention flag up, naming the finding and the
    error, raises an inbox row, and holds the order in `plan_review` so re-accepting that
    finding retries the filing — the one case in which an already-decided finding may be
    decided again (§5.2).
    """
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
        # The ONE re-decision allowed: a finding whose decision stands but whose orders
        # did not file (§5.2).
        if key in accepted_keys and f.get("filing_error"):
            continue
        raise OpsError(
            f"{key!r} is already {f['status']} on {io_id} — a decision is taken once. "
            f"`jarvis io show {io_id}` for what was decided."
        )

    created: list[dict[str, Any]] = []
    learnings: list[str] = []
    errors: list[dict[str, str]] = []
    for key in accepted_keys:
        f = by_key[key]
        if not f.get("knowledge_id"):
            # Through `learn_add` and never `CentralStore.add_knowledge` — §5.4: it is
            # what attributes the write and records the timeline side effect
            # (kn-652456b8). A retry after a filing failure finds the id already here
            # and writes no second entry.
            entry = learn_add(findings.knowledge_text(f), project=name,
                              topic="os-failure-mode", tags=io_id,
                              wo_id=fo.get("plan_wo_id") or "")
            f["knowledge_id"] = entry["id"]
            learnings.append(entry["id"])
        filed = f.setdefault("created_orders", [])
        done = {o.get("index") for o in filed}
        error = ""
        for i, order in enumerate(f.get("proposed_orders") or []):
            if i in done:
                continue
            target = (order.get("project") or "").strip() or name
            order_type = order.get("type") or "work"
            # FIRST LINE names the improvement order: the worker sees this description
            # and nothing else, and this is the readable half of the back-link (§5.3.1).
            description = (
                f"Filed from improvement order {io_id} — run `jarvis io show {io_id}` "
                f"for the root cause this fixes.\n\n{order.get('description', '')}"
            )
            try:
                if order_type == "feature":
                    row = create_feature_order(target, order.get("title", ""),
                                               description=description,
                                               metadata={ORIGIN_IO_KEY: io_id})
                else:
                    row = create_work_order(target, order.get("title", ""),
                                            description=description, parent_id=None,
                                            metadata={ORIGIN_IO_KEY: io_id})
            except OpsError as e:
                error = str(e)
                errors.append({"key": key, "error": error})
                break
            filed.append({"index": i, "id": row["id"], "type": order_type,
                          "title": order.get("title", ""), "project": target})
            created.append({"id": row["id"], "type": order_type,
                            "title": order.get("title", "")})
        if error:
            f["filing_error"] = error
        else:
            f.pop("filing_error", None)
        # The DECISION is written once. A retry of a finding that failed to file touches
        # `created_orders` and `filing_error` and nothing else — §5.2: the decision
        # stands, and re-stamping it would replace the user's reasoning and the moment
        # they gave it with the retry's empty feedback and today's clock.
        if f.get("status") != "accepted":
            f.update(status="accepted", decided_by=decided_by, decided_at=db.now(),
                     feedback=feedback)

    if rejections:
        neo = NeoStore()
        try:
            for key, reason in rejections.items():
                f = by_key[key]
                f.update(status="rejected", decided_by=decided_by, decided_at=db.now(),
                         feedback=reason)
                neo.add_learning(
                    findings.rejection_learning(fo, f, reason, decided_by),
                    project=name, source="review")
        finally:
            neo.close()

    pending = [f for f in report["findings"]
               if (f.get("status") or "pending") == "pending"]
    stuck = [f for f in report["findings"] if f.get("filing_error")]
    status = "completed" if not pending and not stuck else "plan_review"
    reason = ""

    store = ProjectStore(path)
    try:
        store.update_feature_order(io_id, plan=db.to_json(report))
        # THE FLAG MOVES ONLY HERE, at the transition, and nothing re-derives it on a
        # tick — kn-089de524: a flag written on a re-deriving path re-raises itself and
        # overwrites the user's ack.
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
        else:
            store.flag_feature_attention(io_id, findings.review_headline(fo, report))
        if fo.get("plan_wo_id"):
            store.add_event(fo["plan_wo_id"], "findings_reviewed", {
                "improvement_order": io_id, "by": decided_by,
                "accepted": accepted_keys, "rejected": sorted(rejections),
                "created": [c["id"] for c in created],
                "errors": [e["error"] for e in errors],
            })
    finally:
        store.close()

    if stuck:
        # §5.2: a filing failure reaches the user's SINKS, not just a return value
        # nobody reads.
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


def show_feature_order(fo_id: str, project_name: str | None = None) -> dict[str, Any]:
    """The feature order, its plan and its children — the tree, in one call."""
    from . import plans

    name, path, fo = find_feature_order(fo_id, project_name)
    store = ProjectStore(path)
    try:
        plan = db.from_json(fo.get("plan"), None)
        children = [
            {**{k: c[k] for k in ("id", "title", "status", "needs_attention",
                                  "attention_reason", "pr_url", "superseded")},
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
        # The other session that belongs to this feature without being a piece of its
        # work. Shaped exactly like the planner and returned next to it, because the two
        # answer the same question — who is holding this feature, and where do I go to
        # read them. None for every feature planned with validation off, which is all of
        # them until someone turns it on.
        mgr = store.manager_work_order(fo_id)
        manager = ({k: mgr[k] for k in ("id", "title", "status", "result_summary")}
                   if mgr else None)
        return {
            "project": name, **fo,
            "plan": plan,
            "plan_text": "\n".join(plans.render_plan(plan)) if plan else "",
            "planner": planner,
            "manager": manager,
            # The feature's OWN rounds, never its children's: a child's review is on the
            # child's page. Empty for every unit that has never been validated, and it
            # is the emptiness the surfaces branch on — no rounds, no section.
            "validation_rounds": validation_rounds(store, fo_id=fo_id),
            # The feature's OWN follow-ups — the ones its plan review raised. A child's
            # are on the child, where the round that raised them is.
            "issues": issue_index(store, fo_id),
            # Every finding ABOUT this feature, whatever carried it, on the same
            # always-present rule as the rounds — and the rows themselves, exactly as
            # `jarvis wo show --json` carries a work order's (§6). By subject, never by
            # carrier: a child's own alarm belongs on the child.
            "alarms": store.alarms_for_feature(fo_id),
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

    # The spec is snapshotted NOW, from the planner's own tree, because the children never
    # see that tree: it rides in the stored plan, and dispatch materialises each child's
    # own section from it. Refusing a dangling name here costs the planner one revision;
    # accepting it would cost every child a brief pointing at a document none of them has.
    #
    # The content is also the other half of the validation — `parse_plan` cannot resolve a
    # section or find the agent profile without it, and this is the one place that holds
    # both. Reported together with a second `PlanError` shape so a planner fixes
    # everything in one revision, which is `PlanError`'s whole argument.
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
    spec_problems = plans.spec_problems(plan, plan["design_doc_content"])
    if spec_problems:
        raise OpsError(
            f"the plan was not accepted, and nothing was created. Fix all of these and "
            f"resubmit:\n  - " + "\n  - ".join(spec_problems)
        )

    # The planner is who Neo's question hangs off: it is a real work order, it is who
    # receives a rejection, and it is what `jarvis neo list` can link back to. A feature
    # order whose planner was deleted still submits — the question just names the feature
    # order instead of a row that no longer exists.
    planner_id = fo.get("plan_wo_id") or fo_id
    question = plans.build_plan_question(fo, plan)
    neo = NeoStore()
    try:
        q = neo.ask(name, planner_id, question, kind="plan")
        # A resubmission moves `plan_question_id` off the previous review, and
        # `review_plan` only ever closes the one it currently points at — so an
        # escalated plan question survived every revision that followed it (production
        # questions 67 and 130, the second still asking for the user three days after
        # its successor was approved). Close it here, naming what replaced it, because
        # this is the only moment that knows both ids.
        if fo.get("plan_question_id"):
            neo.supersede(
                fo["plan_question_id"],
                f"SUPERSEDED by question {q['id']}",
                f"the plan was revised and resubmitted; question {q['id']} reviews the "
                f"version that replaced the one this asks about",
            )
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
            # THE FEATURE'S BASE, recorded at the only moment it is knowable: the default
            # branch's head just before its first child could start. Everything between
            # this sha and the default branch later IS the feature, by construction, with
            # no per-child bookkeeping to keep in step. Recorded whether or not validation
            # is enabled, because the flag can be turned on while a feature is running and
            # a base nobody wrote down cannot be recovered afterwards.
            store.set_feature_status(fo_id, "executing",
                                     base_sha=evidence.default_branch_head(path) or None)
            store.clear_feature_attention(fo_id)
            # The feature's own agent type, built from the spec's `Agent profile`
            # appendix. Written here so it exists before the first child can be claimed,
            # and rebuilt at every dispatch anyway (`worker_session.feature_agent`) — so
            # this is the fast path, not the only one.
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


def rebuild_feature_agent(fo_id: str,
                          project_name: str | None = None) -> dict[str, Any]:
    """Rewrite a feature's agent type from its stored spec. `jarvis fo agent`.

    The spec snapshot outlives the agent — it is in the plan, which is never deleted — so
    a settled feature can hand its persona back to a session opened by hand, and a live
    one can be repaired without waiting for its next dispatch.
    """
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
    # A feature cancelled while its plan was still under review leaves that review with
    # nothing to decide — the plan it reviews will never be released either way.
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


#: The most of a `--fix` that becomes the corrective child's title. The rest is the
#: description, which is all the worker actually reads.
FIX_TITLE_CHARS = 120


def resume_feature_order(fo_id: str, fix: str = "",
                         project_name: str | None = None) -> dict[str, Any]:
    """`jarvis fo resume` — put a failed feature order back to work.

    The user's own route past a dead child, so that reviving a feature never needs
    somebody with database access. Design: docs/superpowers/specs/2026-08-29-feature-order-resume.md.

    Three things, in this order, and the order is what makes a crash safe:

    1. **Supersede every child that is currently dead.** They stop settling the feature
       either way (`Daemon.settle_features`) and the record of the decision — which
       children, when, and the user's words — goes in `feature_orders.metadata`.
    2. **Back to `executing`, flag cleared.**
    3. **File `fix` as a new child**, if one was given.

    A crash between 2 and 3 leaves a feature that simply settles on what its children
    already say, which is the same answer INV-FEATURE-FALSE-FAILURE would reach. The
    opposite order would file a child under a feature the user had not yet reopened.

    `--fix` is OPTIONAL EVEN WHEN A CHILD IS DEAD. Forcing one would be the OS insisting
    that a cancelled child must always be replaced, and sometimes the honest answer is
    that the feature no longer needs it.

    `failed` only. `cancelled` was the user's own decision and reversing it is a
    different act with different consequences for the children they stopped; `completed`
    has nothing to resume.
    """
    from .invariants import dead_feature_children

    name, path, fo = find_feature_order(fo_id, project_name)
    # An improvement order has no children to revive — §2.4 of the improvement-orders spec.
    _require_kind(fo, "feature", "jarvis io show")
    if fo["status"] != "failed":
        raise OpsError(
            f"{fo_id} is {fo['status']}, not failed — `fo resume` revives a feature a "
            f"child killed. Nothing to resume."
        )
    store = ProjectStore(path)
    try:
        children = store.feature_children(fo_id)
        dead = dead_feature_children(children)
        if dead:
            store.supersede_children(fo_id, [c["id"] for c in dead], note=fix)
        store.set_feature_status(fo_id, "executing")
        store.clear_feature_attention(fo_id)
        child = None
        if fix.strip():
            title = " ".join(fix.split())[:FIX_TITLE_CHARS]
            # `store.create_work_order`, not `ops.create_work_order`: the latter refuses a
            # parent that is not open, and this call IS the reopening — the guard would be
            # reading the status one statement before it stopped being true.
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
    """Refuse a seat name no learning may carry, BEFORE anything is written.

    An unknown seat is a typo, and a typo that is accepted writes a learning into a
    prefix no seat will ever read — invisible, and indistinguishable from the lesson
    having been lost.

    `LEARNING_SCOPES`, not `SEATS`: the supervisor scopes its learnings by the same
    column without being a panel seat, and `SEATS` is what a catalog roster is parsed
    against. `neo_review` still refuses `--seat supervisor` — the supervisor never opines
    on a Neo question, so its existing "did this seat see the question" check catches it,
    and with a message that says which seats did.
    """
    from .neo_store import LEARNING_SCOPES

    if seat and seat not in LEARNING_SCOPES:
        raise OpsError(f"unknown seat {seat!r} — the seats are: "
                       f"{', '.join(LEARNING_SCOPES)}")


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
    # TWO KINDS HAVE NO WORKER TO FORWARD A CORRECTION TO, for different reasons, and
    # both are commands the OS itself tells the user to run. Stated rather than left to
    # the lookup below failing: relying on an accidental `OpsError` is exactly the shape
    # kn-4edb0eb7 warns gets missed. The learning above still lands either way — that is
    # the entire point of the correction.
    #
    # `triage` carries no work order at all (issue #240): there is nobody to send to.
    # `assumption` has one, and that is worse. The worker finished before the question
    # was even filed, and the order is typically `waiting_pr_merge` or `validating` — NOT
    # terminal — so without this the correction would start a turn on an order nobody
    # asked to reopen, the one act this whole mechanism is fenced against
    # (`Daemon._deliver_assumption_verdict`'s inbox row is what sends them here).
    if not approved and q.get("kind") in ("triage", "assumption"):
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
    """The command that really decides an alarm question, naming the alarm when it can.

    Best-effort by design: this only ever builds the tail of a refusal, so a project that
    has moved or a work order that has gone must degrade to the general command rather
    than replace one error with another.
    """
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
    """The command that really decides a bug-priority question. `_alarm_review_hint`'s
    twin, and best-effort for the same reason: it only ever builds a refusal's tail."""
    from . import issues

    url = (issues.triage_payload(q) or {}).get("issue_url") or "<issue-url>"
    return (f"it is on the tracker, so start work on it with: "
            f"{issues.START_COMMAND.format(url=url)}")


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
        # AN ALARM QUESTION HAS NO WORKER TO ANSWER. Nobody asked it — the supervisor
        # did, about a turn the worker was never told anything about — so the delivery
        # below would push a message into a session that is still burning money, which
        # is the exact cost the alarm exists to report. Refused HERE rather than only in
        # the template, because `jarvis neo answer` reaches this too. See §3 of
        # docs/superpowers/specs/2026-08-31-the-supervisor.md.
        if q.get("kind") == "alarm":
            raise OpsError(f"neo question {question_id} is a cost alarm, and answering "
                           f"it would message the worker mid-turn — "
                           f"{_alarm_review_hint(q)}")
        # AN ASSUMPTION QUESTION HAS NO WORKER TO ANSWER EITHER, and for the mirror
        # reason: nobody asked it, and the worker finished long before it was filed. A
        # reply here would start a turn on a work order nobody asked to reopen. The
        # answer the user actually means is a verdict on the assumption, and that has its
        # own command. Refused here as well as in the template, because `jarvis neo
        # answer` reaches this too.
        if q.get("kind") == "assumption":
            raise OpsError(
                f"neo question {question_id} is an assumption review, and the worker "
                f"that recorded it has finished — answering here would reopen it. "
                f"Decide the assumption instead: jarvis wo review {q['wo_id']} "
                f"[--reject] --feedback \"...\"")
        # A TRIAGE QUESTION HAS NO WORKER TO ANSWER EITHER, and for a sharper reason
        # than the alarm's: its `wo_id` is EMPTY (issue #240), so the delivery below
        # would look up a work order that was never created. What the user is really
        # deciding is whether the bug is worth a work order, and that decision is
        # `jarvis issues start`. Refused HERE as well as in the template, because
        # `jarvis neo answer` reaches this too (kn-4edb0eb7).
        if q.get("kind") == "triage":
            raise OpsError(f"neo question {question_id} is a bug-priority "
                           f"re-assessment, and no work order is waiting on it — "
                           f"{_triage_promote_hint(q)}")
        neo.record_answer(question_id, answer, answered_by="user")
        neo.review(question_id, approved=True)  # user-authored ⇒ nothing to review
    finally:
        neo.close()
    # A relay surface like `wo send`: this text IS the user's, typed at `jarvis neo
    # answer` or the dashboard, and the prefix already tells the worker so.
    delivery = send_message(q["wo_id"], f"[Answer from the user] {answer}",
                            project_name=q["project"], relay=True)
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


#: Refused when `JARVIS_WO_ID` is unset, rather than merely unscoped: a contest is the
#: WORKER's exit and nobody else's. Spec 2026-09-12 §8.
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
    """`(wo_id, command)` from either spelling of a gate exit: `<wo> "<cmd>"` or `<id>`.

    Why a request number at all, why it is SCOPED to `caller_wo_id`, and why
    `require_caller` withdraws the no-caller carve-out for `contest` alone: spec
    2026-09-12 §8 and `CONTEST_NEEDS_AN_OWNER`.
    """
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
    """(Workers) ask for permission to run a privileged command, making the case for it.

    The gate fires either way — running the command directly files a request too — but
    this route carries a justification and evidence, and the reviewer sees only what is
    written here. A worker that asks first is far more likely to be approved.
    """
    from . import gates
    from .neo_store import NeoStore

    name, path, wo = find_work_order(wo_id, project_name)
    # No case, no request. This is the whole point of the hold: a reviewer is never
    # handed a privileged action nobody argued for, and the one command that can make
    # that argument must not be the thing that files an unargued one.
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
            # The case goes onto the STANDING request, never a second one: that would be
            # reviewer-shopping (kn-76b155a0), and dropping the text is issue 185 — the
            # failure that made the gate's own printed advice unfollowable.
            held = existing["status"] == gates.AWAITING_CASE
            neo = NeoStore()
            try:
                gates.amend_request(store, neo, wo, action, existing,
                                    justification=why, evidence=evidence)
                if held:
                    # The case has arrived, so the request can finally be seen. This is
                    # the moment the reviewer's question is written, and it is written
                    # from the amended row — which is why it never contains a placeholder.
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
    """(Workers) dispute a gate MATCH: this command performs no privileged action.

    Same reviewer, same queue, same `learn_from_dismissal` on the way out; what differs
    is the claim on the record — a candidate DISMISSAL, never a request for permission.
    See docs/superpowers/specs/2026-09-12-contesting-a-gate-match.md §1.
    """
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
        # Naming the command checked matters: a grant is scoped to an exact string.
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
        # Open and not yet contested, and nothing else — reviewer-shopping otherwise
        # (kn-76b155a0). Spec 2026-09-12 §8.
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
                # Onto the STANDING row, never a second one (kn-76b155a0). The claim
                # changed, so the reviewer's page is rewritten to match it.
                held = existing["status"] == gates.AWAITING_CASE
                approval = gates.amend_request(store, neo, wo, action,
                                               store.mark_contested(existing["id"]),
                                               justification=why)
                if held:
                    gates.queue_for_review(store, neo, name, wo, action, approval)
            else:
                # No hold: a contest IS the case the reviewer needs — spec §1.
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
    # `awaiting_case` decides too. The hold keeps NEO from ruling on a request nobody
    # argued; the user is not Neo — they can read the command, and the alternative is a
    # dead end where the only way out is waiting for the TTL to abandon it.
    if approval["status"] not in ("pending", gates.AWAITING_CASE):
        raise OpsError(
            f"approval {approval_id} is already {approval['status']}"
            + (f" (by {approval['decided_by']})" if approval["decided_by"] else "")
        )
    # Refused here rather than coerced, because there is a person to tell; the daemon's
    # path coerces instead. Spec 2026-09-12 §2.
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
        # The flag this takes down is the one an ESCALATED gate raised, and the user has
        # just answered it. A `self_heal` denial is the one case where the flag standing
        # after the verdict is not stale: `remedies.record_verdict` has just raised it
        # for the alarm, whose symptom the refusal did not address, and clearing it here
        # would put it down for good — nothing re-derives a live alarm in
        # `invariants.true_blockers`.
        if approval["kind"] != gates.SELF_HEAL:
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
                # `awaiting_case` counts as outstanding here even though no reviewer
                # holds it: a request invisible on the one list that shows open gates is
                # a privileged action nobody can find.
                wo_id, statuses=("pending", gates.AWAITING_CASE) if pending_only else None
            )
        finally:
            store.close()
        out.extend({**r, "project": name} for r in rows)
    out.sort(key=lambda r: r["ts"], reverse=True)
    # A held request is the one status whose next event is a CLOCK, so the clock travels
    # with the row: a surface that can only say "awaiting case" leaves the reader with no
    # way to tell a request the worker is about to argue from one nothing will ever
    # close. See `gates.sweep_unargued`.
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
    """Each project's `gates.case_ttl_seconds`, empty when the catalog is unreadable.

    One catalog read for a whole listing; callers fall back to
    `gates.DEFAULT_CASE_TTL_SECONDS`, which is what `Daemon.abandon_unargued_gates` would
    use anyway for a project with no gate block.
    """
    try:
        catalog = resolve_catalog()
    except (OpsError, CatalogError):
        return {}
    return {spec.name: spec.gates.case_ttl_seconds
            for spec in catalog.projects if spec.gates}


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
        "classifier": classifier_stats(),
    }


def classifier_stats() -> dict[str, int]:
    """How often the recogniser has been wrong, and how often nobody stayed to say so.

    Two numbers, reported side by side and never added together — spec 2026-09-12 §5.
    """
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

    `command` may be a request NUMBER instead, which is how a blocked worker can reach
    this at all — §8.

    The diagnostic that a false positive used to require reading source code to get. A
    gate record holds the exact string that fired, so pasting it here is a mechanical
    two-minute answer to "why was this blocked" — which is the difference between
    reporting a classifier defect and guessing at one.
    """
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
        "gate_paperwork": gate_paperwork(command),
        # Per SEGMENT, so a filing reached through an interpreter or written in more than
        # one command shows up as what it is — issue #233.
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


# -- the config console ---------------------------------------------------------------
# docs/superpowers/specs/2026-08-27-the-config-console.md §3, §7, §8. The version ledger
# in `os.db` is the record; the catalog file is a materialised view of it. Every write
# below goes through `_commit_document`, which is this feature's whole write path.

# Which reads of a setting a change actually reaches (§4.2), by resolved path. First
# glob wins, so the two blocks that stay hot whatever their fields are called come before
# the name-based `next-dispatch` rules — `os.neo.model` is hot, `os.defaults.model` is not.
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
    ("*.autocompact_window", "next-dispatch"),
    ("*.append_system_prompt", "next-dispatch"),
    # Read once per spawn, into the settings file that spawn passes to `--settings`
    # (`dispatch._write_worker_settings`). A running worker's session already holds the
    # servers and skills it was launched with; nothing re-reads this at it.
    ("*.wiring.*", "next-dispatch"),
    # Same reason as `wiring`, one layer down: the cap reaches the hook as
    # `JARVIS_SUMMARY_MAX_WORDS`, written into the settings file at spawn
    # (`dispatch._write_worker_settings`). A worker already running keeps the cap it
    # was launched with, and nothing re-reads the catalog at it — the hook must not,
    # since it runs on every Bash command.
    ("*.concision.*", "next-dispatch"),
)

APPLY_NOTES = {
    "hot": "in force on the daemon's next tick",
    "next-dispatch": "applies to work orders dispatched from now on — a worker already "
                     "running keeps what it was dispatched with",
    "restart": "NOT in force until `jarvis start` restarts the OS",
}

# `settings_overrides` is `restart` for a reason the class name does not carry: nothing
# re-runs `bootstrap_project`, so the project's own settings file is untouched.
SETTINGS_OVERRIDES_NOTE = (
    "nothing re-runs bootstrap_project, so the project's .claude settings on disk are "
    "unchanged until `jarvis start` writes them"
)


def apply_class(path: str) -> str:
    """`hot`, `next-dispatch` or `restart` for one resolved path (§4.2)."""
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
    """Does this path change what a worker is ALLOWED to do, rather than what it costs?

    Buys exactly two things (§7): a louder line on the way past, and a mandatory
    `--reason` on the version row.
    """
    return any(fnmatch.fnmatch(path, glob) for glob in SAFETY_KEYS)


def _refuse_worker_write(verb: str) -> None:
    """A worker may not change the fleet's own configuration (§7).

    THIS is the layer that stops one. `ProjectSpec.gates` is empty by default, so on an
    ungated project the `config_write` gate protects nobody, and
    `hooks.preflight_decision` allows any `jarvis` command chain outright.
    """
    wo_id = os.environ.get("JARVIS_WO_ID")
    if not wo_id:
        return
    raise OpsError(
        f"{wo_id} is a worker session — a worker may not `jarvis config {verb}`. "
        "The fleet's configuration is the user's. Ask on the work order record "
        "(`jarvis wo ask`), or file it: `jarvis backlog add jarvis_os \"…\"`."
    )


def _catalog_file(catalog_path: str | None = None) -> Path:
    """The file `jarvis config` rewrites: the one registered at `jarvis start`."""
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
    """Rewrite the catalog from the canonical document, atomically.

    The temp file is a SIBLING of the catalog: `os.replace` is atomic within one
    filesystem and raises across two, so a temp file under /tmp turns the rename into a
    failure on any machine whose $JARVIS_HOME is a separate mount.
    """
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(config_version.canonicalise(document) + "\n", encoding="utf-8")
        os.replace(tmp, path)
    except OSError as e:
        tmp.unlink(missing_ok=True)
        raise OpsError(f"cannot write the catalog at {path}: {e}") from e


def _resolved_of(document: dict[str, Any], path: Path | None = None) -> dict[str, Any]:
    """Parse-and-flatten a document, raising `OpsError` rather than `CatalogError`."""
    try:
        cat = parse_catalog(json.loads(json.dumps(document)), source_path=path)
    except CatalogError as e:
        raise OpsError(str(e)) from None
    return config_version.resolve(cat)


def _commit_document(document: dict[str, Any], *, path: Path, actor: str,
                     reason: str, changes: list[dict[str, Any]]) -> dict[str, Any]:
    """Validate, rewrite the file, write the version row — in that order (§3).

    The order is the decision, not the steps: a row written first leaves a head version
    nobody is running, which nothing detects, while a file written first leaves a running
    config with no record, which the drift check catches and `config adopt` repairs.
    """
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
    """The CLI hands values in as text. JSON first, bare string otherwise: `true`, `3`,
    `null` and `["a"]` mean what they look like, and `opus` is the string it looks like.
    """
    try:
        return json.loads(text)
    except ValueError:
        return text


def _project_names(document: dict[str, Any]) -> list[str]:
    return [p["name"] for p in document.get("projects", [])
            if isinstance(p, dict) and isinstance(p.get("name"), str)]


def _key_path(path: str, project: str | None, document: dict[str, Any]) -> str:
    """The key-space path a user's `<path>` and optional `<project>` name together.

    The positional SCOPES the path (Neo, question 175): `set proj_a validation.enabled`
    means `projects.proj_a.validation.enabled`. An already-absolute path is accepted
    beside a project positional when the two agree and refused when they do not, so no
    spelling ever silently edits a project other than the one named.
    """
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
    """The container object and final key a key-space path names in the raw document.

    The key space is flat (`projects.<name>.…`) and the document is not — `projects` is
    a LIST, addressed by each entry's `name`. A `None` container means the path is not
    written in the file at all, which is a plain fact about a setting on its default and
    not an error: the caller has better words for it than this walk does.
    """
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


#: A write that moved the DOCUMENT and not the effective value, so `old == new` by
#: construction: `pinned` writes into the file a value that was already in force,
#: `unpinned` takes one back out. Neither may be reported as `changed` — that claims a
#: change nobody made — and neither demands a `--reason` on a safety key (§7).
DOCUMENT_ONLY_KINDS = ("pinned", "unpinned")


def _retry(verb: str, path: str, project: str | None, value: str = "") -> str:
    """The exact command to re-run, spelled the way the user spelled it."""
    words = ["jarvis", "config", verb, *([project] if project else []), path]
    if value:
        words.append(value)
    return " ".join(words) + ' --reason "…"'


def _one_change(key: str, before: dict[str, Any], after: dict[str, Any],
                doc_before: Any, doc_after: Any, existed: bool,
                *, removed: bool = False) -> list[dict[str, Any]]:
    """The single triple a `set`/`unset` asked for, read off the RESOLVED maps.

    Resolved rather than raw so the history shows the default the user was actually on
    rather than a blank. A path the resolver does not know — a forward-compatible key
    `parse_catalog` ignores — has no resolved value at all, so it falls back to the
    document's own before/after, which is the only honest answer for it.
    """
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
    """A safety key demands a `--reason` only when its EFFECTIVE value moves (§7).

    The reason exists to go on the version row, so a write that records no row — the
    document already said this — has nowhere to put one; and a write that only pins an
    already-effective value into the file has nothing to justify, since what a worker is
    ALLOWED to do did not move, only where that permission is written down.
    """
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
    """A version by id, or by any unambiguous prefix of one — the ids are 16 hex
    characters and nobody retypes one in full."""
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
    """Does a version's change list reach `project`'s effective configuration?

    An `os.` change does — project settings resolve against `os.defaults` at parse time
    — so the filter only drops versions that exclusively touch OTHER projects. A version
    with no recorded changes is kept: nothing rules it out.
    """
    if not project or not paths:
        return True
    return any(not p.startswith("projects.") or p.startswith(f"projects.{project}.")
               for p in paths)


def _in_scope(resolved: dict[str, Any], project: str | None) -> dict[str, Any]:
    """A project's own settings plus the fleet settings it runs under."""
    if not project:
        return resolved
    prefix = f"projects.{project}."
    return {k: v for k, v in resolved.items()
            if k.startswith(prefix) or k.startswith("os.")}


def set_config(path: str, value: Any, project: str | None = None, *, reason: str = "",
               catalog_path: str | None = None, actor: str = "user") -> dict[str, Any]:
    """Write one setting: validate, rewrite the catalog, record the version (§3)."""
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
    """Remove one setting from the document, so it falls back to its default."""
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
    """One setting as the fleet would read it, and whether the file says so."""
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
    """Which of the resolved paths the DOCUMENT itself says, as against a default of
    this build — `jarvis config get`'s "set in the catalog" answer, for every key at
    once (§8).

    The ledger cannot answer this: `adopt` diffs against nothing and so records every
    resolved path as a change, which would make every shipped default on a freshly
    adopted catalog read as one somebody chose.
    """
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
    """The effective configuration, and where it came from.

    Without `--version` the answer is read from the FILE, because the file is what the
    fleet runs; the head version and the drift flag are the provenance beside it.
    """
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
    # DOCUMENTS, not ids — the third reader of this comparison, and it was the odd one
    # out: a release-rebase row is addressed by document AND build (§6.1), so an id
    # comparison reports drift for ever after an upgrade that moved a default, on a
    # file nobody has touched. `invariants.check_config_drift` and `adopt_config` both
    # say so in as many words; this one quietly disagreed with them.
    drift = head is None or config_version.canonicalise(
        head["document"]) != config_version.canonicalise(document)
    return {"source": "file", "catalog": str(file), "project": project,
            "resolved": in_scope, "version": head,
            "file_version": live_id,
            "written": _written_paths(document, in_scope),
            "drift": drift}


def wiring_config(project: str | None = None,
                  catalog_path: str | None = None) -> Any:
    """The `WiringConfig` a scope runs under — a project's, or the fleet's base.

    The CATALOG OBJECT rather than the resolved map, because a deselection is a
    read-modify-write of a list and `_parse_wiring` is what has already applied the
    project's inheritance from `os.wiring`.
    """
    catalog = resolve_catalog(catalog_path)
    if project is None:
        return catalog.os.wiring
    return project_spec(catalog, project).wiring


def wiring_show(project: str | None = None, *, refresh: bool = False,
                block: bool = True,
                catalog_path: str | None = None) -> dict[str, Any]:
    """What the user has configured, and which of it this scope wires.

    The user's own Claude configuration is READ here and never written: this is the
    populate half of the feature, and the only half that touches it at all.

    `block=False` is the dashboard's call: it takes whatever has been read and lets a
    re-read run behind the page (`wiring.cached`), because the MCP half of discovery is
    a network round trip per server. `reading` is then True and `items` empty, which is
    a different claim from "you have nothing configured" and renders as one.
    """
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
    """Wire or unwire one thing for one scope — `jarvis config set` underneath.

    A lever rather than a path, so no caller has to know that unwiring a plugin appends
    to a list while unwiring the claude.ai connectors flips a flag; `wiring.lever_setting`
    owns that, and the page and the CLI therefore cannot disagree about it (kn-4ea33fe6).
    """
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
    """The ledger, newest first, each row flagged `head` if it is the applied one."""
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
    """Every path where two stored versions disagree, over the RESOLVED maps — each
    document's defaults were frozen at write time, so this survives a release that moved
    one (§2)."""
    left, right = _find_version(a), _find_version(b)
    return {"a": {k: left[k] for k in ("id", "ts", "actor", "reason")},
            "b": {k: right[k] for k in ("id", "ts", "actor", "reason")},
            "changes": config_version.diff(left["resolved"], right["resolved"])}


def restore_config(version_id: str, *, reason: str = "",
                   catalog_path: str | None = None,
                   actor: str = "user") -> dict[str, Any]:
    """Put an old document back. Writes FORWARD: the restored id becomes the head.

    Content addressing means no row is written — the id already exists — so what moves is
    the head pointer, and the history shows the restored version as head beside its
    original write.
    """
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
    """Record a hand-edited catalog as a version, so the record catches up with the file.

    Content-addressed, the way `_seed_gate_rules` is: a file that already hashes to the
    head version writes nothing and says so (§3).

    The one write path that does NOT demand a `--reason` for a safety key, because it is
    the one that changes nothing: the edit already happened on disk and the fleet is
    already running it. Refusing here would leave the record behind the file, which is
    the drift this command exists to close.
    """
    _refuse_worker_write("adopt")
    file = _catalog_file(catalog_path)
    document = _read_document(file)
    resolved = _resolved_of(document, file)
    central = CentralStore()
    try:
        head = central.head_config_version()
    finally:
        central.close()
    # DOCUMENTS, not ids: a release-rebase row is addressed by document AND build
    # (§6.1), so an id comparison would re-adopt the same file after every upgrade.
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


def _turn_usage(store: ProjectStore, turn: dict[str, Any],
                previous: dict[str, Any] | None = None) -> dict[str, Any] | None:
    """A settled turn's recorded usage envelope, lazily (re-)derived from its outfile.

    Three migrations run through this one seam, and all are lazy for the same reason —
    the outfile is still on disk for the overwhelming majority of turns, so history is
    recoverable on demand and, once written back, outlives the file:

    * Rows reaped before `usage_json` existed carry NULL.
    * Rows written before `USAGE_SCHEMA_VERSION` 2 carry token totals read from the
      result envelope's top-level `usage` object, which counts a fraction of the turn
      (see `claude_cli.derive_turn_usage`). They are re-derived rather than trusted: a
      table holding two incompatible counts in one column cannot be summed at all.
    * Version-2 rows read `modelUsage` as the turn's own spend, which it stopped being
      at CLI 2.1.277. Re-deriving them needs `previous`, so this must be walked in
      order — `_turn_rows` does, and it is the only caller that should.

    `cost_usd` IS REPAIRED WITH THE TOKENS, on the row and not only in the envelope:
    that column is what `budget.spent` sums and what a dispatch computes the next turn's
    ceiling from, so leaving it cumulative would keep stopping orders at a third of the
    budget their user set long after the bill stopped saying so.

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
    result = claude_cli.read_turn_result(Path(turn["outfile"]), previous=previous)
    if result is None or not result.usage:
        return stored
    store.set_turn_usage(turn["id"], db.to_json(result.usage),
                         cost_usd=result.cost_usd)
    turn["cost_usd"] = result.cost_usd
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
        # Which reading billed this turn (`project_store.COST_FROM_*`). The CLI's own
        # figure and the transcript floor are not the same currency, and a surface that
        # shows the number owes the reader which one it is (issue #471).
        "cost_source": turn.get("cost_source"),
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
    """Every turn of one work order, in order — and in order because it has to be: each
    envelope is derived against the one before it (`_turn_usage`)."""
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

    from .bill import _cold_prefix_floor

    session = usage_mod.read_session(wo.get("session_id") or "", _cold_prefix_floor(),
                                     index=index)
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


def inspect_config(project: str | None = None) -> Any:
    """The `jarvis inspect` settings in force for `project` — or the OS's — or defaults.

    `ops.validation_config`'s shape and its reasoning: best-effort, because a report over
    files on disk must not fail because a catalog has moved. Unlike validation it falls
    back to `InspectConfig()` rather than to None — every default here is a threshold
    with a measured justification, and having none would mean having no report.
    """
    from .catalog import InspectConfig

    try:
        catalog = resolve_catalog()
        return (catalog.os.inspect if project is None
                else catalog.project(project).inspect)
    except (OpsError, CatalogError, OSError, ValueError):
        return InspectConfig()


def inspect_config_at(project_path: Path) -> Any:
    """`inspect_config` for the project rooted at `project_path`.

    For the caller that holds a `ProjectStore` and no project name —
    `invariants.parked_reason`, which is reached from `true_blockers` and therefore from
    every surface that asks what a work order needs. Falls back to the OS block, then to
    the shipped defaults, for the reason `inspect_config` gives: a threshold with no
    catalog behind it is still a threshold, and having none would mean having no check.
    """
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
    """Will the OS decide this project's assumptions ITSELF, rather than the user?

    `Daemon.auto_review`'s two guards, read by path for the caller that holds a store and
    no name (`inspect_config_at`'s reason). Both, because Neo is the reviewer: a fleet
    with `os.neo.enabled` off files questions nothing drains, so its assumptions are the
    user's after all. One catalog read, so the two guards cannot answer separately.

    False on any unreadable catalog — the safe direction here is the user being asked for
    a decision the OS might have taken, never the reverse.
    """
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
    """THE ONLY WAY ANYTHING LEARNS WHETHER THIS PROJECT SCHEDULES ANYTHING.

    One resolver with one fallback, called by both surfaces that read a hold
    (`_held_jobs` for `jarvis status`, `invariants.check_schedule_progresses` for
    `jarvis doctor`). They used to resolve separately — one by NAME with a disabled
    fallback, one by PATH with `os.schedule`'s — which meant they could give opposite
    answers about the same project and the OS would contradict itself about a mechanism
    it had been told to stop running.

    BY PATH, because that is the only key both callers hold: an invariant is handed a
    store and no name. `catalog` may be passed by a caller that has already loaded one,
    which is a saved file read per project and not a second code path — the resolution
    and the fallback below are the same either way.

    A PROJECT THE CATALOG DOES NOT LIST FALLS BACK TO `ScheduleConfig()` — DISABLED — and
    deliberately NOT to `catalog.os.schedule`, where `inspect_config_at` and
    `messaging_config_at` go. Those answer "by what threshold shall I judge this work",
    which a fleet-wide default answers perfectly well for a project nobody has configured.
    This answers "is this mechanism supposed to be running here", and for a project absent
    from the catalog the daemon's answer is no: `Daemon.tick` iterates `catalog.projects`,
    so such a project's jobs can never fire and its holds can never clear. Inheriting an
    enabled `os.schedule` would make both surfaces report a permanent hold for a project
    the OS does not drive — §4's failure, reached by a third route.
    """
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
    """`os.messaging` for the project rooted at `project_path`.

    `inspect_config_at`'s twin, for `invariants.stuck_message` — an invariant is handed
    a `ProjectStore` and no project name, and falls back to the OS block and then to the
    shipped defaults for the reason that function gives: a check with no threshold is no
    check.
    """
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


def inspect_report(target: str, project: str | None = None, *,
                   write_floor: int | None = None,
                   join_floor: int | None = None) -> dict[str, Any]:
    """Where a work order's or a feature order's TIME went — `jarvis cost`'s other half.

    Resolves the target exactly the way `_cost_for_target` does, feature order first and
    for the same reason, so the two commands agree about what an id means and a reader
    can put one report beside the other.

    Read-only and no paid call: everything comes from transcripts already on disk. A
    unit whose transcript has expired is reported with `found: false`, the same honest
    gap `jarvis cost` reports, because an unmeasurable clock and an idle one are
    different answers.
    """
    from dataclasses import replace

    from . import holds, inspection
    from . import usage as usage_mod

    index = usage_mod.index_sessions()
    # The catalog decides the floors and the flags override them for one invocation:
    # a project's setting is what the report means by "large" day to day, and `--writes
    # -over` is someone asking a different question of the same session once.
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
        # The OS's own record of what it was holding this order for, so the report can
        # state both clocks and name the difference (`holds`). Two indexed reads.
        spans = holds.held(store, wo["id"])
        anatomy = (inspection.read_session(session, cfg, index=index, spans=spans)
                   if session
                   else inspection.Anatomy(session_id="", holds=list(spans),
                                           write_floor=cfg.report_write_floor,
                                           join_floor=cfg.report_join_floor))
        payload = anatomy.as_dict()
        payload.update(wo_id=wo["id"], project=project_name, title=wo["title"],
                       status=wo["status"], kind=wo.get("kind") or "worker")
        return payload

    try:
        name, path, fo = find_feature_order(target, project)
    except OpsError:
        name, wo_path, wo = find_work_order(target, project)
        cfg = settings(name)
        store = ProjectStore(wo_path)
        try:
            payload = unit(name, wo, store)
        finally:
            store.close()
        return {"scope": wo["id"], "title": wo["title"],
                "write_floor": cfg.report_write_floor,
                "join_floor": cfg.report_join_floor, "units": [payload]}

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
                     for child in store.feature_children(fo["id"]))
    finally:
        store.close()
    cfg = settings(name)
    return {"scope": fo["id"], "title": fo["title"], "status": fo["status"],
            "write_floor": cfg.report_write_floor,
            "join_floor": cfg.report_join_floor, "units": units}


def _alarm_dict(name: str, row: dict[str, Any]) -> dict[str, Any]:
    """The twenty keys `list_cost_alarms` publishes, from one `alarms_across` row.

    Frozen at sixteen by §1 of docs/superpowers/specs/2026-08-31-the-supervisor.md
    (`kn-4d8449f1`) and bound by four surfaces written against it at once, so it lives
    in one function rather than inline: the review reads below build on top of this dict
    and must not be able to drift from it. Anything a review surface needs beyond these
    keys is ADDED by `_reviewable`, never smuggled in here.

    §1 of docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md adds four —
    `source`, `probe`, `subject_kind`, `subject_id` — and the licence stops there.
    `subject_id` is published rather than derived so no surface has to branch to build a
    link.

    TWO OF THE SIXTEEN NOW COME FROM THE SUBJECT AND ONE DELIBERATELY DOES NOT, and that
    split is what lets every template render a feature finding unchanged:

    - `title` and `status` are the SUBJECT's — the feature order's when there is one.
      A reader asked what is wrong; the carrier is plumbing.
    - `live` stays the CARRIER's `needs_attention`, because a feature order's attention
      flag has no `acknowledged_blockers` analogue and is wiped unconditionally at eight
      sites. The ack has to be able to stick, so the flag lives on the work order.

    For every alarm on the tree today the subject IS the carrier, so swapping those two
    rules is a no-op on the whole suite — only a fixture whose feature and carrier carry
    different titles and statuses can tell them apart.
    """
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
    """Every turn the OS raised WHILE it was burning, newest first, across the fleet.

    Read off `wo_alarms` rows since §1 of
    docs/superpowers/specs/2026-08-31-the-supervisor.md; the `cost_alarm` event it used
    to read is still written, and is still the raise's dedupe memory. The row is what
    carries an identity, so an alarm can be linked to, claimed and answered.

    Acking clears the ASK and must not erase the record of what the fleet spent, so
    `live` stays the one derived field: whether this alarm's WORK ORDER is still asking
    for the user, never `alarm_status`. That is why several alarms on one order share it
    — one ack answers all of them, and the page has to be able to say so rather than
    offering four buttons that do the same thing.

    `fo_id` and `sources` are filters, not modes: the unfiltered read still returns
    every finding, feature-subject ones included, with its subject already resolved.
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
            rows = store.alarms_across(limit=limit, wo_id=wo_id, fo_id=fo_id,
                                       sources=sources)
        finally:
            store.close()
        out.extend(_alarm_dict(name, row) for row in rows)
    out.sort(key=lambda r: r["ts"], reverse=True)
    return out[:limit]


# -- the symptom catalogue: what a project is watched for ------------------------------


def supervisor_probes(project_name: str | None = None,
                      catalog_path: str | None = None) -> list[dict[str, Any]]:
    """The health probes in force, each with WHERE ITS ANSWER CAME FROM.

    `kn-42c52cec`'s lesson: a resolved value the user cannot see is a value they cannot
    trust, and probe inheritance is exactly the kind of resolution that goes wrong
    quietly — a project that switches one off looks identical, on every other surface,
    to a project that never had it. `source` is the whole point of the read:

    - `fleet` — the OS list's entry, untouched here;
    - `project override` — the project named this id and changed something, INCLUDING
      disabling it (a disabled probe is present and marked, never absent, so what the
      fleet watches for stays legible);
    - `project addition` — an id the OS list does not have.

    Raises rather than answering `None` on an unreadable catalog: unlike
    `validation_config`, nothing depends on this to keep working — it is a read someone
    typed, and a silent empty list would read as "this project is watched for nothing".

    §2 of docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md.
    """
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
    """{project: {probe id: title}} for the whole fleet, in ONE catalog load.

    Per project rather than one flat map, because a probe id is only unique within a
    project: two projects may define the same id with different words, and a flat map
    would render one project's title over the other's finding.

    ANSWERS {} ON AN UNREADABLE CATALOG rather than raising, which is the opposite of
    `supervisor_probes`' rule and for the opposite reason: that is a read someone typed,
    this one decorates rows that must render regardless. Every surface falls back to the
    probe id, so a missing catalog costs a less readable page and never a page (§6).
    """
    try:
        catalog = resolve_catalog(catalog_path)
    except (OpsError, CatalogError):
        return {}
    return {spec.name: {p.id: p.title for p in spec.supervisor.probes}
            for spec in catalog.projects}


# -- the review loop: what the supervisor decided, and what the user makes of it -------
#
# `list_cost_alarms`' dict is frozen and four surfaces bind it, so the two review reads
# ADD to it rather than widen it (Neo question 197). Both go through `_reviewable`, which
# is the only place the supervisor's reasoning and Neo's advice are assembled — the list
# and the per-alarm page are the same two surfaces `_question.html` exists to keep
# identical for a Neo question.


def _reviewable(name: str, row: dict[str, Any],
                answers: dict[int, dict[str, Any]],
                titles: dict[str, dict[str, str]] | None = None,
                results: dict[str, str] | None = None) -> dict[str, Any]:
    """One alarm as a review surface reads it: the frozen dict plus the reasoning.

    `verdict_reason` is the supervisor's argument and `note` is what it wrote to the
    user; they are different sentences and the page shows both. `neo_advice` is the
    ANSWER text, which lives in neo.db and no per-project read can reach — passed in
    already fetched so this stays a pure projection and the caller opens Neo once for
    the whole queue rather than once per escalated row.

    `remedy_result` arrives the same way and for the same reason: `wo_alarms` records
    that a remedy was proposed and settled, and WHAT IT DID is the `remedy_applied`
    event's payload (§5) — one read per project rather than one per row.
    """
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
        # What the supervisor asked to do about it, and what came of it — §6.
        "remedy": row.get("remedy"),
        "remedy_argument": row.get("remedy_argument"),
        "remedy_approval_id": row.get("remedy_approval_id"),
        "remedy_result": (results or {}).get(row["id"]),
    })
    # The frozen dict carries the probe ID; every review surface renders its TITLE.
    view["kind_label"] = alarm_kind_label(view, titles or {})
    return view


def _neo_answers(question_ids: Sequence[int]) -> dict[int, dict[str, Any]]:
    """Neo's questions by id, in one store open. Empty when nothing escalated."""
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
    """{alarm id: what the remedy actually did}, from the `remedy_applied` events.

    Skipped entirely when no row in hand names a remedy, which is every project on the
    fleet as things ship: the read costs nothing where nothing was ever proposed.

    `wo_id` is the ONE-ALARM read and it is uncapped: a per-alarm page that fell off the
    end of the fleet-wide limit would show a remedy with no outcome, which reads as one
    that never ran. Reversed because the two store reads order oppositely and everything
    below wants newest-first.
    """
    if not any(r.get("remedy") for r in rows):
        return {}
    out: dict[str, str] = {}
    stream = (list(reversed(store.events_of_kind(wo_id, "remedy_applied"))) if wo_id
              else store.events_across("remedy_applied", limit=max(limit, 200)))
    for event in stream:
        payload = db.from_json(event.get("payload"), {}) or {}
        alarm_id = str(payload.get("alarm_id") or "")
        # Newest first, so the first one wins — a grant covers ONE application, but an
        # alarm re-proposed after a refusal could carry two.
        if alarm_id and alarm_id not in out:
            out[alarm_id] = str(payload.get("result") or "")
    return out


def _enriched_alarms(project_name: str | None, limit: int,
                     read: Callable[[ProjectStore], list[dict[str, Any]]]
                     ) -> list[dict[str, Any]]:
    """`read` over every registered project, through `_reviewable`.

    ONE walk for the two fleet-wide review reads. The alternative — a second loop that
    also opens a store, Neo and the catalog — is how the queue and the feed come to
    show the same alarm differently, which is the whole argument for `_reviewable`
    being a single function.
    """
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
    """`list_cost_alarms`' rows as a REVIEW surface reads them, newest first.

    The same filters and the same order, plus everything the frozen dict may not carry:
    the supervisor's argument, Neo's advice, the probe's title and the remedy. A second
    function rather than six more keys on the published dict — `kn-4d8449f1`'s rule,
    and the four surfaces binding that dict are why it holds.
    """
    out = _enriched_alarms(project_name, limit, lambda store: store.alarms_across(
        limit=limit, wo_id=wo_id, fo_id=fo_id, sources=sources))
    out.sort(key=lambda r: r["ts"], reverse=True)
    return out[:limit]


def alarm_review_queue(project_name: str | None = None, limit: int = 200
                       ) -> list[dict[str, Any]]:
    """Alarms the supervisor answered and the user has not yet looked at, newest first.

    `acked` + `unreviewed`, per §5 of the supervisor spec. Deliberately NOT the whole
    unreviewed set: an `escalated` alarm is still with Neo and is asked about by the
    attention flag, so listing it here would ask the user for the same decision twice
    in two different words. A `proposed` one is out for the same reason and one more:
    the way to answer it is `jarvis gate approve|deny`, not a verdict on a verdict.

    An APPLIED remedy lands here, because `remedies.apply` leaves the alarm `acked` —
    "addressed by the supervisor on your behalf" is exactly what this half is (§6).
    """
    out = _enriched_alarms(project_name, limit, lambda store: [
        r for r in store.alarms_across(limit=limit, statuses=("acked",))
        if r["review_status"] == "unreviewed"])
    out.sort(key=lambda r: r["decided_at"] or r["ts"], reverse=True)
    return out[:limit]


def _find_alarm(alarm_id: str, project_name: str | None = None
                ) -> tuple[str, Path, dict[str, Any]]:
    """Locate one alarm across the fleet, shaped as `alarms_across` returns it.

    Joined rather than the bare `wo_alarms` row: an alarm is unreadable without its work
    order's title and status, and going back through `alarms_across` is what keeps this
    read and the fleet-wide one from diverging.
    """
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
    """One alarm in full — what fired, what the supervisor made of it, what Neo said.

    The anchor `/alarms` cannot be: a list has no per-row identity, and both the work
    order's timeline and a Neo escalation's inbox line link straight at one alarm.

    THE PAGE'S ONE EXTRA OVER THE LIST is the gate request behind a proposed remedy —
    its verdict, and the EVIDENCE PACKET the finding cited, which is stored nowhere else
    in the OS: `wo_alarms` keeps the reason, and what the judge was actually shown
    survives only on the `self_heal` approval `remedies.propose` filed (§6).
    """
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
    """The user's verdict on the supervisor's verdict. Modelled on `neo_review`.

    It does NOT message the worker: a corrected Neo answer was advice the worker acted
    on, and an alarm review corrects the supervisor about a turn the worker was never
    told anything about.
    """
    from . import supervisor
    from .neo_store import SUPERVISOR_SEAT, NeoStore

    # Every refusal ahead of the first write, as in `neo_review`: a rejected review
    # leaves the row untouched rather than half-applied.
    if not approved and not feedback.strip():
        raise OpsError("a correction needs feedback — what should the supervisor "
                       "have decided?")
    name, path, row = _find_alarm(alarm_id, project_name)
    if row["alarm_status"] not in ("acked", "escalated"):
        # A REFUSAL THAT DOES NOT SAY WHERE TO GO IS A BUG REPORT. `proposed` is the
        # status a user is most likely to arrive here with — the supervisor is asking
        # them for something — and the thing it asks for is a gate verdict, not a
        # verdict on the verdict (§6).
        where = ""
        if row["alarm_status"] == "proposed":
            gate = row["remedy_approval_id"] or "<id>"
            where = (f" — it is asking permission to {row['remedy'] or 'act'}, so "
                     f"answer it with `jarvis gate approve {gate}` or "
                     f"`jarvis gate deny {gate} --reason \"…\"`")
        raise OpsError(f"alarm {alarm_id} is {row['alarm_status']}, and only an alarm "
                       f"the supervisor has decided ('acked' or 'escalated') can be "
                       f"reviewed{where}")
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
            # THE CORRECTION IS THE MEMORY, AND IT LIVES IN neo.db RATHER THAN ON THE
            # ROW: the alarm is this project's record of one turn and settles with it;
            # what the supervisor learned outlives it and reaches the next review
            # through `supervisor.build_system_prompt` (§6). `seat` scopes it there and
            # keeps it out of Neo's own prompt, which has to stay byte-stable.
            if not approved:
                neo.add_learning(
                    supervisor.learning_from_review(row, feedback.strip()),
                    project=name, source="review", seat=SUPERVISOR_SEAT)

            # THE CLOSE SITE NOBODY ELSE OWNS. An escalated alarm holds an open Neo
            # question; the user deciding the alarm here IS its answer, and leaving it
            # open would go on asking them for a ruling they have just given.
            # `supersede` is guarded on the question still being open, so a real verdict
            # is never overwritten.
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


# -- what the knowledge base costs, and who actually reads it -----------------------------

#: How much of a work order's own title has to survive into a search for the "it could
#: have looked" signal to mean anything. Below this the query is words like "fix the",
#: which match half the base and would manufacture a miss for every silent order.
MISSED_MIN_WORDS = 3


def _index_cost(central: CentralStore, name: str, path: Path) -> dict[str, Any]:
    """What the knowledge base costs a dispatch prompt, measured rather than estimated.

    The same prompt is built twice, with the index and without it, and the difference IS
    the cost — no model of what the block "should" be, so it cannot drift away from what
    `build_worker_prompt` actually emits.
    """
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


#: The side-effect kinds a packet can carry, and what each one is called in the timeline
#: event that records it. ONE dict rather than two literals so a kind cannot be added to
#: the collector and forgotten in the recorder.
#:
#: Only knowledge writes are here. GitHub state, backlog items, Neo learnings, gate
#: outcomes and configuration changes are the same class of invisible effect and are
#: deliberately out of this release (issue #200 step 3, "decide separately") — the shape
#: is a list of typed records precisely so adding one is a collector change and not a
#: packet change.
SIDE_EFFECT_EVENTS = {
    "knowledge_added": "knowledge_added",
    "knowledge_retracted": "knowledge_retracted",
}


def learn_add(content: str, *, project: str = "", topic: str = "", tags: str = "",
              wo_id: str = "") -> dict[str, Any]:
    """Write a knowledge entry AND record on the work order that it did.

    Here rather than in `CentralStore.add_knowledge` because the two halves live in
    different databases: the knowledge base is central and a timeline is per-project, so
    the writer needs a `ProjectStore` the leaf store cannot reach without importing this
    module. `find_work_order` is how the project is resolved from the id alone.

    **The timeline half is best effort and the knowledge half is not.** An entry that was
    written must not be reported as failed because its work order has since been deleted
    — the entry is the durable thing and the event is the record of it.
    """
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
    """Retire a knowledge entry AND record on the work order that it did.

    The mirror of `learn_add`, and the case that produced issue #200: wo-28405ea1
    retracted a fleet-wide instruction every future worker reads, and nothing anywhere
    recorded that it had.

    Raises exactly what `CentralStore.retract_knowledge` raises — `KeyError` for an
    unknown id, `ValueError` for a missing reason or an already-retired entry — so the
    CLI's error handling is unchanged.
    """
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
    """One side-effect event on a work order's timeline. False if it could not land.

    Swallows everything: this is called AFTER the durable write has already happened, so
    an exception escaping here would report a completed change as a failure.
    """
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
    """Knowledge-base writes and retractions attributed to this work order (issue #200).

    Read from the knowledge base's own attribution columns rather than from the timeline
    events `_record_side_effect` writes: the event is best effort and the row is not, so
    an entry whose event failed to land is still judged.

    `detail` carries the WHOLE entry, not a headline. A reviewer asked to judge a
    retraction cannot do it from a summary line — the question is whether the text that
    was retired deserved to be, and that needs the text.
    """
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


#: What a staged release carries on the work order's timeline, newest source first, and
#: what the marker's `state` would have said at that point. See `_release_effects` for
#: why reading only one of the two sources has a gap.
_RELEASE_EVENT_STATES = (("release_verified", "verified"),
                         ("release_restart", "restarting"))


def _release_effects(store: ProjectStore, wo_id: str) -> list[dict[str, Any]]:
    """The release this work order shipped, if it shipped one. §4 and §5 of spec
    docs/superpowers/specs/2026-09-17-a-round-with-nothing-to-judge.md.

    A release authors nothing in the worker's worktree: the version bump and the tag are
    made in a throwaway worktree on a branch nobody merges, and everything else it did
    happened to origin, to the production checkout and to systemd. So the packet was
    empty, the panel escalated, and autonomous shipping needed a human (wo-ec96a1e9).

    TWO SOURCES FOR THE CLAIM, because neither covers the whole window. `--stage` writes
    the marker and no timeline event, so before the restart the marker is all there is;
    `verify_on_boot` DELETES the marker on success, so a round still pending across that
    daemon restart would otherwise collect nothing and escalate a release that had
    already landed. The union has no gap.

    **BOTH SOURCES ARE CLAIMS, NOT PROOF, AND THE DIFFERENCE IS THE WHOLE OF `verified`.**
    A marker is a JSON file under `$JARVIS_HOME/run/` and a timeline event is a row; a
    work order that delivered nothing could write either one and hand itself an attested
    effect, settling `completed` with no flag and nobody having reviewed it — exactly what
    void must never be reachable by (review round 1). Neither `wo_id` inside the marker
    nor the kind of an event carries provenance: the timeline has no author column, so
    "only the daemon writes these kinds" is a convention and not a check.

    So the claim is measured against state this order could not author
    (`release.verify_release_claim`): the production checkout, THIS order's own approved
    release gate, and a tag that postdates that approval. Production alone is not enough —
    it stays on the last release until the next one, so a work order that delivered
    nothing could name the version already live and replay it for free (review round 2).

    The effect is COLLECTED either way — it belongs on the record, and a packet that
    carries it is one the panel can judge — but it is `verified` only when that
    cross-check passes, and an unverified effect is never attested and so never voids.

    Raises nothing on absence, which is the registry's rule: see `side_effects_of`.
    """
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
        # Consumed by the registry, which turns it into `attested`. See `side_effects_of`.
        "verified": not unverified,
    }]


@dataclass(frozen=True)
class SideEffectCollector:
    """One kind of durable change no diff can show, and whether a reviewer can judge it.

    `attested` is OPT-IN and defaults to False: a collector added tomorrow that has not
    thought about the question gets its effects JUDGED, never silently voided.

    IT IS A CEILING AND NOT A STAMP, which is the correction review round 1 forced. The
    flag says this collector MAY produce machine-verified effects; whether a PARTICULAR
    effect is one is the collector's per-effect `verified`, because the artifact a
    collector reads is often a claim the submitter could have written (see
    `_release_effects`). The registry ANDs the two, and a collector that sets `attested`
    on its own records has it overwritten — which is what lets
    `evidence.side_effects_digest` leave the field out of the hash.
    """

    name: str
    collect: Callable[[ProjectStore, str], list[dict[str, Any]]]
    attested: bool = False


#: THE REGISTRY. Adding a kind of invisible effect is an entry here, not another widening
#: of `daemon.py`'s empty-packet guard — which has now been widened twice for the same
#: lesson (issue #200, then wo-ec96a1e9). Spec §1.
SIDE_EFFECT_COLLECTORS = (
    SideEffectCollector("knowledge", _knowledge_effects),
    SideEffectCollector("release", _release_effects, attested=True),
)


def side_effects_of(store: ProjectStore, wo_id: str) -> list[dict[str, Any]]:
    """Durable, non-file change this work order made, for its evidence packet.

    NOTHING IS SWALLOWED HERE, deliberately. A collector that raises leaves the round
    unjudged and retried on the next tick, which is right; catching it would drop a
    JUDGEABLE effect from a packet that could then read as all-attested and void. So a
    collector must return `[]` for "nothing of my kind", never raise.

    `attested` is computed HERE and only here: the collector's opt-in ANDed with that
    effect's own `verified`, which is consumed rather than carried so exactly one flag
    reaches the packet. Absent `verified` means not verified, so the fail-safe direction
    is the default in both halves.
    """
    effects: list[dict[str, Any]] = []
    for collector in SIDE_EFFECT_COLLECTORS:
        for effect in collector.collect(store, wo_id):
            record = {k: v for k, v in effect.items() if k != "verified"}
            record["attested"] = collector.attested and bool(effect.get("verified"))
            effects.append(record)
    return effects


def feature_side_effects(store: ProjectStore, fo_id: str) -> list[dict[str, Any]]:
    """Every child's side effects, in the feature's own child order.

    A feature whose children delivered only knowledge changes hits the same empty
    guard its children would have — `daemon._validate_feature_order` escalates on
    `not packet.files` too — so the feature packet carries the union.
    """
    effects: list[dict[str, Any]] = []
    for child in store.feature_children(fo_id):
        for effect in side_effects_of(store, str(child["id"])):
            effects.append({**effect, "wo_id": str(child["id"])})
    return effects


def knowledge_usage_report(project: str | None = None, days: int | None = None,
                           limit: int = 20) -> dict[str, Any]:
    """What memory costs and whether anyone uses it.

    Three questions, answered from three different places because no one of them can
    answer another:

    * COST — `_index_cost` builds a real dispatch prompt with and without the index. The
      body text of the base is reported beside it as what the index AVOIDS: the entries
      never reach a prompt, so the base's size is not the prompt's size (kn-1485b845).
    * USE — the `knowledge_reads` log, written by the CLI verbs a worker runs. Before it
      existed this half of the report did not exist at all.
    * NON-USE — the work orders that completed having never read anything, and of those,
      the ones whose own title matches an entry that already existed when they started.
      A title match is EVIDENCE, NOT A VERDICT, and it is labelled that way wherever it
      is rendered: the same search a worker would have run is what scores it, so it
      inherits that search's blind spots — synonyms, since FTS5 landed (bl-8169af54).
    """
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
        # Nothing before the log's first row was OBSERVED, so nothing before it can be
        # reported as an order that ignored the knowledge base.
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
                    # Only entries that already existed when the order was created: an
                    # entry it wrote ITSELF is not something it failed to read.
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
            # An entry whose first line overflows the headline reaches the index as a
            # sentence cut mid-word, and the index is the only thing that decides
            # whether it is ever read.
            "truncated_headlines": sum(
                1 for e in entries
                if len(e["content"].split("\n", 1)[0]) > len(headline(e["content"]))),
        },
        "prompt_cost": cost,
        "reads": summary,
        "read_chars_per_order": (round(summary["chars"] / summary["orders"])
                                 if summary["orders"] else 0),
        # When the read log begins. Every "never read" and "never looked" figure below is
        # a statement about work AFTER this instant and about nothing before it.
        "observed_from": observed_from,
        "top_entries": top[:limit],
        "never_read": [e for e in top if e["reads"] == 0][:limit],
        "never_read_count": sum(1 for e in top if e["reads"] == 0),
        "silent_orders": silent[:limit], "silent_order_count": len(silent),
        "could_have_read": missed[:limit], "could_have_read_count": len(missed),
    }


# -- budgets -----------------------------------------------------------------------
#
# The verbs behind `jarvis wo budget` and `jarvis fo budget`. Setting a budget is an
# ordinary write; RAISING one on an order that has already stopped for it is the thing
# these exist to make possible, and the measured fact that makes it possible is that a
# turn stopped by `--max-budget-usd` leaves a complete, resumable transcript behind
# (src/jarvis/budget.py). So a top-up does not restart the work, it continues it.


def work_order_budget(wo_id: str, project_name: str | None = None) -> dict[str, Any]:
    """What this work order's ceiling is and what it has spent. Read-only."""
    name, path, wo = find_work_order(wo_id, project_name)
    store = ProjectStore(path)
    central = CentralStore()
    try:
        cap = budget.ceiling(store, central, wo)
        spend = budget.spent(store, central, wo_id)
        # The turn in flight, which the enforcement's two queries cannot see and the
        # reader can (issue #471, Neo question 469). Added for DISPLAY only, and it
        # never reaches `cap`.
        live = budget.in_flight(store, wo_id)
        parent_pool = None
        if wo.get("parent_id"):
            try:
                parent_pool = budget.pool(store, central,
                                          store.get_feature_order(wo["parent_id"]))
            except KeyError:
                parent_pool = None
    finally:
        central.close()
        store.close()
    return {
        "project": name, "wo_id": wo_id, "title": wo["title"], "status": wo["status"],
        "budget_usd": wo.get("budget_usd"),
        "reserved_usd": wo.get("budget_reserved_usd"),
        "worker_usd": spend.worker_usd, "jarvis_usd": spend.jarvis_usd,
        "spent_usd": spend.total_usd,
        # RECORDED plus IN FLIGHT, and the two are kept apart rather than merged: one is
        # the CLI's own figure and governs the ceiling, the other is a list-price
        # estimate off the live transcript and governs nothing. A surface showing
        # `live_spent_usd` owes the reader the `~`.
        "in_flight_usd": live,
        "live_spent_usd": spend.total_usd + live,
        "cap_usd": cap.cap_usd if cap else None,
        "cap_source": cap.source if cap else None,
        "remaining_usd": cap.remaining_usd if cap else None,
        "live_remaining_usd": (cap.cap_usd - spend.total_usd - live) if cap else None,
        "feature_unreserved_usd": parent_pool.unreserved_usd if parent_pool else None,
    }


def set_work_order_budget(wo_id: str, amount: float | None,
                          project_name: str | None = None) -> dict[str, Any]:
    """Set, change or clear a work order's budget — and resume it if it had stopped.

    `amount=None` clears the ceiling: the order runs uncapped again, which is what every
    order does by default.

    THE RESUME IS THE POINT. A budget that can only ever stop work is a worse OS than no
    budget at all, so raising the number on a `budget_exhausted` order puts it back to
    work in the SAME session, with its context and its half-finished work intact. The
    turn that was cut off is relaunched by `worker_session.retry`'s rules — a nudge when
    the conversation already reached the model, the original prompt when it did not — so
    the worker is never told to redo what it has already done.

    It refuses to resume on a number that is not enough to resume ON. Setting $5 on an
    order that has already spent $6 leaves it exactly where it is, with a message saying
    so, rather than launching a turn the CLI would stop on its first call and charging
    the user for the privilege.

    A CHILD OF A FEATURE IS RE-CUT BEFORE IT IS JUDGED. Its ceiling is the tighter of its
    own budget and the slice its feature reserved for it, so raising the budget alone
    would leave the stale slice winning the `min` and the order stuck for ever. The
    re-cut spends the feature's CURRENT unreserved remainder, which is the money the
    family actually has — and when that is nothing, the note says so and names the
    feature, because "top up the child" and "top up the feature" are then different acts.
    """
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
        cap = budget.ceiling(store, central, fresh)
        spend = budget.spent(store, central, wo_id)
        if fresh["status"] == budget.EXHAUSTED:
            if cap is not None and cap.exhausted:
                note = (f"still over its ceiling — {budget.format_usd(spend.total_usd)} "
                        f"spent against {budget.format_usd(cap.cap_usd)}")
                if cap.source == "feature":
                    note += (" (its feature's slice, and the feature has "
                             f"{budget.format_usd(_feature_unreserved(store, central, fresh))}"
                             " unreserved — raise the feature with `jarvis fo budget`)")
            else:
                resumed, note = _resume_after_budget(store, name, fresh)
    finally:
        central.close()
        store.close()
    return {"project": name, "wo_id": wo_id, "title": wo["title"],
            "budget_usd": amount, "previous_usd": wo.get("budget_usd"),
            "spent_usd": spend.total_usd, "resumed": resumed, "note": note}


def _feature_unreserved(store: ProjectStore, central: CentralStore,
                        wo: dict[str, Any]) -> float:
    """What this child's feature has that no live child has claimed. 0.0 if it has none.

    Neo's condition on reserve-on-dispatch, reached from the other end: the escalation
    states it when the child stops, and this states it again when a top-up of the child
    could not find any money to give it.
    """
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
    """Put a topped-up work order back to work, or say why it could not be.

    Best effort, and a failure is REPORTED rather than raised: the budget has been raised
    either way, and the daemon's own loops reach the order again on the next tick now
    that it is out of `budget_exhausted`. Losing the new number because a relaunch could
    not happen this second would be the worse outcome.
    """
    from . import worker_session

    turn = store.latest_turn(wo["id"])
    if turn is None:
        # Never dispatched — it stopped before its first turn ran. `pending` is where the
        # dispatch loop picks it up, and it reserves a fresh slice on the way in.
        store.set_status(wo["id"], "pending")
        store.clear_attention(wo["id"])
        return True, "back on the dispatch queue"
    try:
        spec = project_spec(resolve_catalog(), project_name)
    except OpsError as e:
        store.set_status(wo["id"], "pending")
        store.clear_attention(wo["id"])
        return False, f"budget raised, but the catalog could not be read ({e})"
    pause = worker_session.TurnPause(
        # Constructed, never diagnosed: `turn_pause` returns None for a budget stop
        # (worker_session._reap files it without a `reason` precisely so the retry sweep
        # can never pick it up). This exists only to hand `retry` the two things it needs
        # — the turn to relaunch, and a reason to word a nudge from.
        reason=worker_session.PAUSE_BUDGET, turn=turn, retry_at=0.0, attempts=1,
        message="its budget was raised",
    )
    store.set_status(wo["id"], "running")
    store.clear_attention(wo["id"])
    try:
        fresh = worker_session.retry(store, spec, wo, pause)
    except budget.BudgetExhausted as e:
        budget.escalate(store, store.get_work_order(wo["id"]), e.exhausted)
        return False, "still has no headroom — it stopped again immediately"
    except Exception as e:  # noqa: BLE001 — see the docstring
        store.set_status(wo["id"], "pending")
        return False, f"budget raised, but the relaunch failed ({e})"
    store.add_event(wo["id"], "budget_resumed",
                    {"budget_usd": wo.get("budget_usd"), "turn": fresh["seq"]})
    return True, f"resumed at turn {fresh['seq']}"


def set_feature_budget(fo_id: str, amount: float | None,
                       project_name: str | None = None) -> dict[str, Any]:
    """Set, change or clear a FEATURE order's family budget.

    Raising it takes the feature out of `budget_exhausted` on the next reconcile tick
    (`Daemon.settle_features`). Its CHILDREN stay parked in their own exhausted state
    until each is topped up: the family has money again, but which child gets it is the
    user's call, and silently re-funding every one of them would spend the new budget on
    whatever happened to be running rather than on what the user meant to rescue.

    `exhausted_children` in the result is that list, and it is the whole instruction —
    `jarvis wo budget <child> <amount>` re-cuts the child's slice out of the money this
    just added, and resumes it. Nothing here writes a reservation: doing it from this end
    would have to guess the split, which is the guess the user came to make.
    """
    name, path, fo = find_feature_order(fo_id, project_name)
    store = ProjectStore(path)
    central = CentralStore()
    try:
        store.update_feature_order(fo_id, budget_usd=amount)
        p = budget.pool(store, central, store.get_feature_order(fo_id))
        stuck = [c["id"] for c in store.feature_children(fo_id)
                 if c["status"] == budget.EXHAUSTED]
    finally:
        central.close()
        store.close()
    return {"project": name, "fo_id": fo_id, "title": fo["title"],
            "budget_usd": amount, "previous_usd": fo.get("budget_usd"),
            "spent_usd": p.spent_usd if p else None,
            "unreserved_usd": p.unreserved_usd if p else None,
            "exhausted_children": stuck}
