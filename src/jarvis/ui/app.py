"""Jarvis web dashboard — server-rendered, zero JS, reads the same stores and calls
the same ops functions as the CLI. Binds to localhost by default (no auth in MVP)."""

from __future__ import annotations

import time
from pathlib import Path

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.templating import Jinja2Templates

from .. import ops
from ..central_store import CentralStore
from ..daemon import daemon_running
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


def fmt_age(ts: float | None) -> str:
    if not ts:
        return "–"
    d = time.time() - ts
    for limit, unit, div in ((90, "s", 1), (5400, "m", 60), (129600, "h", 3600)):
        if d < limit:
            return f"{int(d / div)}{unit}"
    return f"{int(d / 86400)}d"


def create_app() -> FastAPI:
    app = FastAPI(title="Jarvis", docs_url=None, redoc_url=None)
    templates = Jinja2Templates(directory=str(TEMPLATES))
    templates.env.globals.update(
        status_meta=STATUS_META, origin_meta=ORIGIN_META,
        level_tone=LEVEL_TONE, fmt_age=fmt_age,
    )

    def render(request: Request, template: str, active: str = "dashboard",
               **ctx) -> HTMLResponse:
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
        return templates.TemplateResponse(request, template, ctx)

    # -- pages ------------------------------------------------------------------

    @app.get("/", response_class=HTMLResponse)
    def dashboard(request: Request):
        st = ops.os_status()
        return render(request, "dashboard.html", st=st, refresh=15)

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
        finally:
            store.close()
        show_debug = debug not in ("", "0", "false")
        return render(request, "work_order.html", project=pname, wo=wo,
                      timeline=build_timeline(wo, events, messages,
                                              include_debug=show_debug),
                      debug=show_debug, debug_count=count_debug(events),
                      messages=messages, assumptions=assumptions)

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
                      escalated=escalated, unreviewed=unreviewed,
                      history=history, learnings=learnings)

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
    def review(name: str, wo_id: str, decision: str = Form(...)):
        ops.review_work_order(wo_id, accept=(decision == "accept"))
        return RedirectResponse(f"/wo/{name}/{wo_id}", status_code=303)

    @app.post("/wo/{name}/{wo_id}/cancel")
    def cancel_wo(name: str, wo_id: str):
        ops.cancel(wo_id)
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
