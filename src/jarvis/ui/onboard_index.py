"""The way in: drafts in progress and the mimic picker (docs/superpowers/specs/2026-11-09-onboarding-from-the-dashboard.md §6)"""

from __future__ import annotations

from typing import Callable
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import ops


def register(app: FastAPI, render: Callable[..., HTMLResponse]) -> None:
    def index(request: Request, *, form: dict[str, str] | None = None, error: str = "",
              status_code: int = 200) -> HTMLResponse:
        drafts = ops.onboard_drafts()
        projects = sorted(ops.registered_project_paths().items())
        drafted = {d["name"] for d in drafts}
        return render(request, "onboard_index.html", active="onboard",
                      status_code=status_code, drafts=drafts,
                      projects=[n for n, _ in projects],
                      revise=[(n, str(p)) for n, p in projects if n not in drafted],
                      form=form or {"path": "", "name": "", "like": "", "fresh": ""},
                      error=error)

    @app.get("/onboard", response_class=HTMLResponse)
    def onboard_index(request: Request):
        return index(request)

    @app.post("/onboard", response_class=HTMLResponse)
    def onboard_open(request: Request, path: str = Form(""), name: str = Form(""),
                     like: str = Form(""), fresh: str = Form("")):
        """`ops.onboard_open`; a refusal re-renders with the typed fields kept (§6)"""
        form = {"path": path, "name": name, "like": like, "fresh": fresh}
        if not path.strip():
            return index(request, form=form, error="repository path is required",
                         status_code=400)
        try:
            view = ops.onboard_open(path.strip(), name.strip() or None,
                                    like=like or None, fresh=bool(fresh))
        except ops.OpsError as e:
            return index(request, form=form, error=str(e), status_code=400)
        return RedirectResponse(f"/onboard/{quote(view['project']['name'])}",
                                status_code=303)
