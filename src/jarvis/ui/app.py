"""Jarvis web dashboard — server-rendered, zero JS, reads the same stores and calls
the same ops functions as the CLI. Binds to localhost by default (no auth in MVP)."""

from __future__ import annotations

import time
import traceback
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import ops
from ..central_store import CentralStore
from ..daemon import daemon_running
from ..paths import PRODUCTION, deployment_env, logs_dir
from ..project_store import ProjectStore
from ..timeline import build_timeline, count_debug

TEMPLATES = Path(__file__).parent / "templates"

STATUS_META = {
    "pending":       {"word": "pending",     "icon": "◌", "tone": "muted"},
    "dispatching":   {"word": "dispatching", "icon": "◍", "tone": "active"},
    "running":       {"word": "running",     "icon": "●", "tone": "active"},
    "waiting_input": {"word": "waiting on you", "icon": "◉", "tone": "warn"},
    "needs_review":  {"word": "needs review",   "icon": "◭", "tone": "warn"},
    "completed":     {"word": "completed",   "icon": "✓", "tone": "ok"},
    "failed":        {"word": "failed",      "icon": "✗", "tone": "bad"},
    "cancelled":     {"word": "cancelled",   "icon": "–", "tone": "muted"},
}
ORIGIN_META = {
    "jarvis": {"word": "jarvis", "framework": True},
    "ui":     {"word": "ui",     "framework": True},
    "manual": {"word": "manual", "framework": False},
    "adhoc":  {"word": "ad-hoc", "framework": False},
}
LEVEL_TONE = {"info": "muted", "warning": "warn", "critical": "bad"}
# Privileged-action gates. `pending` splits in two on the page — with Neo (costs the
# user nothing) vs escalated to the user — so it carries the neutral mark here.
GATE_META = {
    "pending":  {"word": "pending",  "icon": "◌", "tone": "warn"},
    "approved": {"word": "approved", "icon": "✓", "tone": "ok"},
    "denied":   {"word": "denied",   "icon": "✗", "tone": "bad"},
    "expired":  {"word": "expired",  "icon": "–", "tone": "muted"},
}

# How often the dashboard re-reads OS state. Not a page reload — the browser swaps
# the live regions in place (see dashboard.html), so in-progress typing survives.
REFRESH_SECONDS = 15


def fmt_age(ts: float | None) -> str:
    if not ts:
        return "–"
    d = time.time() - ts
    for limit, unit, div in ((90, "s", 1), (5400, "m", 60), (129600, "h", 3600)):
        if d < limit:
            return f"{int(d / div)}{unit}"
    return f"{int(d / 86400)}d"


def log_ui_error(request: Request, exc: BaseException) -> None:
    """Append a dashboard failure to `$JARVIS_HOME/logs/ui.log`.

    Uvicorn already prints the traceback on stdout, but in production that is the
    systemd journal — outside the OS's own state directory, so neither `jarvis`
    commands nor the agents reading `logs/` can see that the UI ever broke. The
    daemon keeps `jarvisd.log` next to the databases for exactly this reason; the
    dashboard now does the same.
    """
    try:
        d = logs_dir()
        d.mkdir(parents=True, exist_ok=True)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
        tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        with (d / "ui.log").open("a") as f:
            f.write(f"{stamp} [ERROR] {request.method} {request.url.path} — "
                    f"{type(exc).__name__}: {exc}\n{tb}")
    except Exception:  # noqa: BLE001 — logging must never mask the original failure
        pass


def instance_badge() -> dict[str, str | bool]:
    """Which Jarvis this dashboard is driving, for the header.

    Production and development are the same code in two checkouts on one machine
    (docs/DEPLOYMENT.md), and their dashboards are otherwise identical — so the badge
    is the only thing stopping someone from acting on the live fleet while believing
    they are in the dev sandbox. Both facts are read once per process: neither the
    environment nor the installed version can change under a running server.

    The version is the *installed* one, never a constant in the source: on `main` the
    version string deliberately lags the shipped tag (only release branches carry the
    bump), so a literal would be wrong in exactly the place it matters most.
    """
    from ..bugreport import jarvis_version
    env, detail = deployment_env()
    try:
        version = jarvis_version()
    except Exception:  # noqa: BLE001 — see gate_badge: a badge must not 500 a page
        version = "unknown"
    return {"env": env, "prod": env == PRODUCTION, "version": version,
            "label": "prod" if env == PRODUCTION else "dev",
            "detail": f"{env} · {detail} · version {version}"}


def gate_badge() -> int | None:
    """How many gates are waiting on the user, for the nav.

    Only escalated ones count: a request Neo is still reviewing is deliberately
    free of charge, and badging it would undo the point of having Neo. Never
    raises — a badge must not be the reason a page 500s.
    """
    try:
        return len([g for g in ops.list_gates(pending_only=True)
                    if g["escalated"]]) or None
    except Exception:  # noqa: BLE001 — see docstring
        return None


def create_app() -> FastAPI:
    app = FastAPI(title="Jarvis", docs_url=None, redoc_url=None)

    @app.middleware("http")
    async def no_store(request: Request, call_next):
        # Every page reflects live OS state (inbox acks, work order status); a
        # browser serving a stale copy from disk cache or history (bfcache) after
        # the user navigates back would show notifications as still unacked.
        response = await call_next(request)
        response.headers["Cache-Control"] = "no-store"
        return response

    templates = Jinja2Templates(directory=str(TEMPLATES))
    templates.env.globals.update(
        status_meta=STATUS_META, origin_meta=ORIGIN_META, gate_meta=GATE_META,
        level_tone=LEVEL_TONE, fmt_age=fmt_age, instance=instance_badge(),
    )

    def render(request: Request, template: str, active: str = "dashboard",
               status_code: int = 200, **ctx) -> HTMLResponse:
        from ..neo_store import NeoStore
        ctx["active"] = active
        ctx["daemon_up"] = daemon_running() is not None
        neo = NeoStore()
        try:
            c = neo.counts()
        finally:
            neo.close()
        ctx["neo_badge"] = (c.get("escalated", 0) + c.get("failed", 0)
                            + c.get("unreviewed", 0)) or None
        ctx["gate_badge"] = gate_badge()
        return templates.TemplateResponse(request, template, ctx,
                                          status_code=status_code)

    @app.exception_handler(Exception)
    def unhandled(request: Request, exc: Exception) -> HTMLResponse:
        """Last line of defence: a bare "Internal Server Error" tells the user
        nothing, and a dead-end deep link out of a Telegram alert is exactly where
        they land. Name the failure on the page and put the traceback on disk."""
        log_ui_error(request, exc)
        message = (f"Something went wrong loading {request.url.path} — "
                   f"{type(exc).__name__}: {exc}. "
                   "The full traceback is in $JARVIS_HOME/logs/ui.log.")
        try:
            return render(request, "error.html", message=message, status_code=500)
        except Exception:  # noqa: BLE001 — the chrome itself may be what broke
            return HTMLResponse(f"<h1>Something went wrong</h1><p>{message}</p>",
                                status_code=500)

    # -- pages ------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        st = ops.os_status()
        return render(request, "dashboard.html", st=st, refresh=REFRESH_SECONDS)

    @app.get("/project/{name}", response_class=HTMLResponse)
    def project(request: Request, name: str, hidden: str = ""):
        paths = ops.registered_project_paths()
        if name not in paths:
            return render(request, "error.html", message=f"unknown project {name!r}")
        show_hidden = hidden not in ("", "0", "false")
        store = ProjectStore(paths[name])
        try:
            wos = store.list_work_orders(include_hidden=show_hidden)
            hidden_count = sum(
                1 for wo in store.list_work_orders(include_hidden=True) if wo["hidden"]
            )
        finally:
            store.close()
        central = CentralStore()
        try:
            backlog = central.list_backlog(project=name, status="open")
        finally:
            central.close()
        return render(request, "project.html", project_name=name, path=paths[name],
                      wos=wos, backlog=backlog, show_hidden=show_hidden,
                      hidden_count=hidden_count)

    @app.get("/wo/{name}/{wo_id}", response_class=HTMLResponse)
    def work_order(request: Request, name: str, wo_id: str, debug: str = ""):
        try:
            pname, path, wo = ops.find_work_order(wo_id, name)
        except ops.OpsError as e:
            return render(request, "error.html", message=str(e))
        store = ProjectStore(path)
        try:
            events = store.list_events(wo_id)
            messages = store.list_messages(wo_id)
            assumptions = store.pending_assumptions(wo_id)
            # A worker held at a gate looks identical to an idle one from here, so
            # the reason it stopped belongs on the page it stopped on.
            store.expire_approvals()
            approvals = store.list_approvals(wo_id)
        finally:
            store.close()
        show_debug = debug not in ("", "0", "false")
        return render(request, "work_order.html", project=pname, wo=wo,
                      timeline=build_timeline(wo, events, messages,
                                              include_debug=show_debug),
                      debug=show_debug, debug_count=count_debug(events),
                      messages=messages, assumptions=assumptions,
                      approvals=approvals)

    @app.get("/inbox", response_class=HTMLResponse)
    def inbox(request: Request):
        central = CentralStore()
        try:
            items = central.unacked_inbox()
        finally:
            central.close()
        return render(request, "inbox.html", active="inbox", items=items)

    @app.get("/backlog", response_class=HTMLResponse)
    def backlog(request: Request):
        central = CentralStore()
        try:
            items = central.list_backlog(status=None)
            open_ids = {i["id"] for i in items if i["status"] == "open"}
            blockers = {i["id"]: central.unfinished_dependencies(i["id"])
                        for i in items if i["id"] in open_ids}
        finally:
            central.close()
        return render(request, "backlog.html", active="backlog", items=items,
                      blockers=blockers)

    @app.get("/knowledge", response_class=HTMLResponse)
    def knowledge(request: Request):
        central = CentralStore()
        try:
            rows = central.search_knowledge("", limit=200)
        finally:
            central.close()
        return render(request, "knowledge.html", active="knowledge", rows=rows)

    @app.get("/neo", response_class=HTMLResponse)
    def neo_page(request: Request):
        from ..neo_store import NeoStore
        neo = NeoStore()
        try:
            counts = neo.counts()
            # Oldest first: that is the order Neo drains them, and the oldest is the
            # one most likely to be stuck.
            in_flight = list(reversed(
                neo.list_questions(statuses=("queued", "answering"))))
            escalated = neo.list_questions(statuses=("escalated", "failed"))
            unreviewed = neo.list_questions(statuses=("answered",),
                                            review_status="unreviewed")
            unreviewed = [q for q in unreviewed if q["answered_by"] == "neo"]
            history = [q for q in neo.list_questions(limit=100)
                       if q["status"] == "answered"
                       and not (q["answered_by"] == "neo"
                                and q["review_status"] == "unreviewed")]
            learnings = neo.all_learnings(limit=100)
        finally:
            neo.close()
        return render(request, "neo.html", active="neo", counts=counts,
                      in_flight=in_flight, escalated=escalated,
                      unreviewed=unreviewed, history=history, learnings=learnings)

    @app.get("/gates", response_class=HTMLResponse)
    def gates_page(request: Request):
        """Privileged-action approvals. Three states, three different asks of the
        user: escalated ones need a decision, ones still with Neo need nothing (but
        can be pre-empted), decided ones are the audit trail."""
        rows = ops.list_gates(include_request=True)
        pending = [g for g in rows if g["status"] == "pending"]
        return render(request, "gates.html", active="gates",
                      escalated=[g for g in pending if g["escalated"]],
                      with_neo=[g for g in pending if not g["escalated"]],
                      decided=[g for g in rows if g["status"] != "pending"])

    @app.get("/api/status")
    def api_status():
        return JSONResponse(ops.os_status())

    # -- actions (same ops functions as the CLI) --------------------------------------

    @app.post("/wo/create")
    def create_wo(project: str = Form(...), title: str = Form(...),
                  description: str = Form(""), model: str = Form("")):
        try:
            wo = ops.create_work_order(project, title, description=description,
                                       model=model or None, origin="ui")
        except ops.OpsError as e:
            return RedirectResponse(f"/?error={e}", status_code=303)
        return RedirectResponse(f"/wo/{project}/{wo['id']}", status_code=303)

    @app.post("/wo/{name}/{wo_id}/send")
    def send(name: str, wo_id: str, message: str = Form(...)):
        ops.send_message(wo_id, message, source="ui", project_name=name)
        return RedirectResponse(f"/wo/{name}/{wo_id}", status_code=303)

    @app.post("/wo/{name}/{wo_id}/review")
    def review(name: str, wo_id: str, decision: str = Form(...),
               feedback: str = Form("")):
        ops.review_work_order(wo_id, accept=(decision == "accept"), feedback=feedback)
        return RedirectResponse(f"/wo/{name}/{wo_id}", status_code=303)

    @app.post("/wo/{name}/{wo_id}/cancel")
    def cancel_wo(name: str, wo_id: str):
        ops.cancel(wo_id)
        return RedirectResponse(f"/wo/{name}/{wo_id}", status_code=303)

    @app.post("/wo/{name}/{wo_id}/ack")
    def ack_wo(name: str, wo_id: str):
        try:
            ops.ack_attention(wo_id, project_name=name)
        except ops.OpsError as e:
            # The one case that refuses: pending assumptions want a decision, not a
            # dismissal. Say so instead of silently doing nothing.
            return RedirectResponse(f"/wo/{name}/{wo_id}?error={e}", status_code=303)
        return RedirectResponse(f"/wo/{name}/{wo_id}", status_code=303)

    @app.post("/wo/{name}/{wo_id}/hide")
    def hide_wo(name: str, wo_id: str):
        ops.hide_work_order(wo_id, hidden=True, project_name=name)
        return RedirectResponse(f"/project/{name}", status_code=303)

    @app.post("/wo/{name}/{wo_id}/unhide")
    def unhide_wo(name: str, wo_id: str):
        ops.hide_work_order(wo_id, hidden=False, project_name=name)
        return RedirectResponse(f"/project/{name}", status_code=303)

    @app.post("/wo/{name}/{wo_id}/delete")
    def delete_wo(name: str, wo_id: str):
        ops.delete_work_order(wo_id, project_name=name)
        return RedirectResponse(f"/project/{name}", status_code=303)

    @app.post("/wo/{name}/{wo_id}/resume-auto")
    def resume_auto(name: str, wo_id: str):
        ops.resume_in_auto(wo_id, project_name=name)
        return RedirectResponse(f"/wo/{name}/{wo_id}", status_code=303)

    @app.post("/neo/{question_id}/review")
    def neo_review(question_id: int, decision: str = Form(...),
                   feedback: str = Form("")):
        try:
            ops.neo_review(question_id, approved=(decision == "approve"),
                           feedback=feedback)
        except ops.OpsError as e:
            return RedirectResponse(f"/neo?error={e}", status_code=303)
        return RedirectResponse("/neo", status_code=303)

    @app.post("/neo/{question_id}/answer")
    def neo_answer(question_id: int, text: str = Form(...)):
        try:
            ops.neo_answer_escalated(question_id, text)
        except ops.OpsError as e:
            return RedirectResponse(f"/neo?error={e}", status_code=303)
        return RedirectResponse("/neo", status_code=303)

    @app.post("/neo/learn")
    def neo_learn(content: str = Form(...), project: str = Form("")):
        from ..neo_store import NeoStore
        neo = NeoStore()
        try:
            neo.add_learning(content, project=project, source="manual")
        finally:
            neo.close()
        return RedirectResponse("/neo", status_code=303)

    @app.post("/gates/{approval_id}/decide")
    def decide_gate(approval_id: int, decision: str = Form(...),
                    reason: str = Form(""), project: str = Form(""),
                    next: str = Form("")):
        """Open or refuse a gate. Approval ids are per-project autoincrements, so the
        form carries the project the row was rendered from — without it two projects
        holding the same id make `ops.decide_gate` refuse to guess.

        `next` returns the user to the page they decided from (the gates tab or a work
        order). Only same-site paths are honoured: a form field is attacker-settable,
        and an open redirect out of the dashboard is not worth the convenience.
        """
        back = next if next.startswith("/") and not next.startswith("//") else "/gates"
        try:
            ops.decide_gate(approval_id, approved=(decision == "approve"),
                            reason=reason, project_name=project or None)
        except ops.OpsError as e:
            sep = "&" if "?" in back else "?"
            return RedirectResponse(f"{back}{sep}error={e}", status_code=303)
        return RedirectResponse(back, status_code=303)

    @app.post("/inbox/ack")
    def ack(inbox_id: str = Form("")):
        central = CentralStore()
        try:
            central.ack_inbox(int(inbox_id) if inbox_id else None)
        finally:
            central.close()
        return RedirectResponse("/inbox", status_code=303)

    @app.post("/backlog/promote/{item_id}")
    def promote(item_id: str, force: str = Form("")):
        try:
            result = ops.promote_backlog(item_id, force=bool(force))
        except ops.OpsError as e:
            return RedirectResponse(f"/backlog?error={e}", status_code=303)
        return RedirectResponse(f"/wo/{result['project']}/{result['wo_id']}", status_code=303)

    return app
