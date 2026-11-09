"""`POST /onboard/{name}/help`: open a read-only help session (docs/superpowers/specs/2026-11-09-onboarding-from-the-dashboard.md §4)"""

from __future__ import annotations

import shlex
from collections.abc import Callable

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse

from .. import ops


def register(app: FastAPI, render: Callable[..., HTMLResponse]) -> None:
    @app.post("/onboard/{name}/help", response_class=HTMLResponse)
    def onboard_help(request: Request, name: str, section: str = Form(...),
                     focus: str = Form("")) -> HTMLResponse:
        try:
            res = ops.onboard_help(name, section, focus)
        except ops.OpsError as e:
            return render(request, "error.html", message=str(e), status_code=404)
        # A failed spawn is shown with its error and the by-hand command, never a blank success
        return render(request, "onboard_help.html", active="onboard", project=name,
                      section=section, focus=focus, res=res,
                      command=shlex.join(res["argv"]),
                      status_code=502 if res["error"] else 200)
