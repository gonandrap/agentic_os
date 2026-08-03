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
from .central_store import CentralStore
from .daemon import daemon_running
from .invariants import PR_CLOSED_BLOCKER, true_blockers
from .paths import daemon_pidfile, ensure_home, logs_dir
from .project_store import OPEN_STATUSES, ProjectStore


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
    from .invariants import check_catalog, check_project

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

    results, total = [], 0
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
    out = {"repair": repair, "violations": total, "projects": results}
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
                for wo in flagged.values():
                    if wo["id"] in gate_held:
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
                    attention.append(item)
                drift = settings_drift(path / ".claude" / "settings.json")
                projects.append({
                    "name": p["name"], "path": p["path"],
                    "description": p["description"],
                    "summary": summary,
                    "open_work_orders": [
                        {k: wo[k] for k in ("id", "title", "status", "origin",
                                            "needs_attention", "attention_reason",
                                            "pr_url")}
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
        from .neo_store import NeoStore
        neo = NeoStore()
        try:
            neo_counts = neo.counts()
            for q in neo.list_questions(statuses=("escalated", "failed")):
                if q.get("kind") == "approval":
                    # Gate escalations get their own item below, with the command to run.
                    # Reported here too, they would tell the user to `jarvis neo answer`
                    # a question whose real resolution is `jarvis gate approve`.
                    continue
                attention.append({
                    "project": q["project"], "wo_id": q["wo_id"],
                    "title": f"Neo escalated: {q['question'][:80]}",
                    "status": "neo_escalated",
                    "reason": q.get("answer_reason") or "Neo declined to answer for you",
                    "neo_question_id": q["id"],
                })
        finally:
            neo.close()
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
        return {
            "daemon": {
                "running": pid is not None,
                "pid": pid,
                "catalog": central.get_state("catalog_path"),
            },
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
                      backlog_id: str | None = None) -> dict[str, Any]:
    paths = registered_project_paths()
    if project_name not in paths:
        raise OpsError(f"project {project_name!r} not registered "
                       f"(known: {sorted(paths)}). Run `jarvis start` first.")
    store = ProjectStore(paths[project_name])
    try:
        return store.create_work_order(
            title=title, description=description, origin=origin, model=model,
            effort=effort, permission_mode=permission_mode,
            append_system_prompt=append_system_prompt, backlog_id=backlog_id,
        )
    finally:
        store.close()


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


def resume_in_auto(wo_id: str, project_name: str | None = None) -> dict[str, Any]:
    """Recover a worker stalled on a permission prompt: flip it to `auto` mode and
    nudge it to continue. jarvisd delivers the nudge by resume-forking the worker's
    session (reading the now-`auto` mode), so it stops re-prompting on routine tools.
    The nudge clears the attention flag via the normal send path.
    """
    name, path, wo = find_work_order(wo_id, project_name)
    store = ProjectStore(path)
    try:
        previous = wo["permission_mode"]
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
    return {"project": name, "wo_id": wo_id, "permission_mode": "auto",
            "note": "flipped to auto and nudged; jarvisd resumes the worker when idle"}


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


def finish(wo_id: str, summary: str, pr_url: str | None = None) -> dict[str, Any]:
    """The worker reporting its own result.

    `pr_url` is what separates "delivered" from "delivered and merged": a work order
    that ends in a pull request is not finished until a human merges it, so it settles
    into `waiting_pr_merge` and stays on the open list with the link, instead of going
    to `completed` and disappearing into the settled group nobody reads. The user
    closes it with `jarvis wo done` after merging — nothing polls GitHub.

    Pending assumptions still outrank it: those are a decision the OS is waiting on,
    and a PR the user merges before deciding them accepts them by the back door.
    """
    name, path, wo = find_work_order(wo_id)
    store = ProjectStore(path)
    try:
        fields: dict[str, Any] = {"result_summary": summary}
        if pr_url:
            fields["pr_url"] = pr_url
        store.update_work_order(wo_id, **fields)
        if store.pending_assumptions(wo_id):
            store.set_status(wo_id, "needs_review")
            store.flag_attention(wo_id, "assumptions pending review")
            status = "needs_review"
        elif pr_url:
            store.set_status(wo_id, "waiting_pr_merge")
            store.clear_attention(wo_id)
            status = "waiting_pr_merge"
        else:
            store.set_status(wo_id, "completed")
            store.clear_attention(wo_id)
            status = "completed"
        store.add_event(wo_id, "finished", {"summary": summary,
                                            **({"pr_url": pr_url} if pr_url else {})})
    finally:
        store.close()
    if wo.get("backlog_id") and status == "completed":
        central = CentralStore()
        try:
            central.mark_backlog(wo["backlog_id"], "done")
        finally:
            central.close()
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
    """
    name, path, wo = find_work_order(wo_id)
    store = ProjectStore(path)
    try:
        pending = store.pending_assumptions(wo_id)
        for a in pending:
            store.review_assumption(a["id"], "accepted" if accept else "rejected")
        if wo["status"] == "needs_review":
            if accept:
                store.set_status(wo_id, "completed")
                store.clear_attention(wo_id)
            elif not feedback:
                # With feedback the guidance is delivered below, so the work order is
                # not waiting on the user — only a bare rejection strands it.
                store.flag_attention(wo_id, "assumptions rejected — send guidance with `jarvis wo send`")
        store.add_event(wo_id, "reviewed", {"accepted": accept, "count": len(pending),
                                            "feedback": feedback})
    finally:
        store.close()

    out = {"project": name, "wo_id": wo_id, "reviewed": len(pending), "accepted": accept}
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


# -- Neo (OS answerer agent) ---------------------------------------------------------------------

def ask_question(wo_id: str, question: str, project_name: str | None = None) -> dict[str, Any]:
    """(Workers) queue a question for Neo instead of stalling on the user.

    The work order flips to waiting_input WITHOUT flagging user attention — Neo
    exists precisely to keep these off the user's plate. The answer arrives as the
    worker's next user turn via the normal message-delivery path.
    """
    from .neo_store import NeoStore

    name, path, wo = find_work_order(wo_id, project_name)
    context = f"{wo['title']}\n{(wo.get('description') or '')[:800]}"
    neo = NeoStore()
    try:
        q = neo.ask(name, wo_id, question, context=context)
    finally:
        neo.close()
    store = ProjectStore(path)
    try:
        store.add_event(wo_id, "question_asked", {"neo_question_id": q["id"]})
        if wo["status"] == "running":
            store.set_status(wo_id, "waiting_input")
    finally:
        store.close()
    return {"project": name, "wo_id": wo_id, "question_id": q["id"],
            "note": "queued for Neo — end your turn; the answer arrives as your next user turn"}


def neo_status() -> dict[str, Any]:
    from .neo_store import NeoStore
    neo = NeoStore()
    try:
        return neo.counts()
    finally:
        neo.close()


def neo_review(question_id: int, approved: bool, feedback: str = "") -> dict[str, Any]:
    """Review one of Neo's answers. A correction becomes a learning (Neo's own DB)
    and, when the work order is still open, is forwarded to the worker as guidance."""
    from . import neo as neo_mod
    from .neo_store import NeoStore

    if not approved and not feedback.strip():
        raise OpsError("a correction needs feedback — what should Neo have said?")
    neo = NeoStore()
    try:
        q = neo.get(question_id)
        if q is None:
            raise OpsError(f"neo question {question_id} not found")
        if q["status"] != "answered":
            raise OpsError(f"neo question {question_id} is {q['status']}, not answered")
        q = neo.review(question_id, approved, feedback)
        learning = None
        if not approved:
            learning = neo.add_learning(
                neo_mod.learning_from_review(q, feedback),
                project=q["project"], source="review", question_id=question_id,
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
    # The escalation is handled — release the work order from the attention list.
    try:
        _, path, _ = find_work_order(q["wo_id"], q["project"])
        store = ProjectStore(path)
        try:
            store.clear_attention(q["wo_id"])
            store.add_event(q["wo_id"], "escalation_answered",
                            {"neo_question_id": question_id})
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
    try:
        gates.apply_decision(store, approval_id, verdict=verdict,
                             reason=reason or "approved by the user", decided_by="user")
        store.clear_attention(approval["wo_id"])
    finally:
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


# -- backlog ------------------------------------------------------------------------------------

def promote_backlog(item_id: str, force: bool = False) -> dict[str, Any]:
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
        wo = create_work_order(
            item["project"], item["title"], description=item["description"],
            origin="jarvis", backlog_id=item_id,
        )
        central.mark_backlog(item_id, "promoted", promoted_wo_id=wo["id"])
        return {"backlog_id": item_id, "wo_id": wo["id"], "project": item["project"],
                "forced_over_blockers": [b["id"] for b in blockers] if force else []}
    finally:
        central.close()
