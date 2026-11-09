"""Review and apply an onboarding draft (docs/superpowers/specs/2026-11-09-onboarding-from-the-dashboard.md §5)"""

from __future__ import annotations

from typing import Any, Callable
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse

from .. import ops


def grouped(view: dict[str, Any]) -> list[dict[str, Any]]:
    """The diff rows by section, in rail order (§5)"""
    from ..onboard_knobs import area_of
    from .onboard_pages import section_url

    name = view["project"]["name"]
    owner: dict[str, str] = {}
    for s in view["sections"]:
        for k in s["knobs"]:
            owner.setdefault(k["path"], s["key"])
            if k["kind"] == "identity":  # shown as `project.<f>`, diffed as `projects.<name>.<f>`
                owner.setdefault(f"projects.{name}.{k['tail']}", s["key"])

    def section_of(path: str) -> str:
        hit = area_of(path)  # a project-level copy of an os knob still has its area
        return owner.get(path) or (hit[0] if hit and any(
            s["key"] == hit[0] for s in view["sections"]) else "identity")

    by: dict[str, list[dict[str, Any]]] = {}
    for r in view["review"]["rows"]:
        key = section_of(r["path"])
        by.setdefault(key, []).append({**r, "href": section_url(name, key, r["path"])})
    return [{"key": s["key"], "title": s["title"], "rows": by[s["key"]]}
            for s in view["sections"] if s["key"] in by]


def register(app: FastAPI, render: Callable[..., HTMLResponse]) -> None:
    def review(request: Request, name: str, *, error: str = "", reason: str | None = None,
               status_code: int = 200) -> HTMLResponse:
        try:
            view = ops.onboard_view(name)
        except ops.OpsError as e:
            return render(request, "error.html", active="onboard", message=str(e),
                          status_code=404)
        try:
            dry, refusal = ops.onboard_apply(view["draft_path"], dry_run=True), ""
        except ops.OpsError as e:
            dry, refusal = None, str(e)  # §5: shown verbatim, never paraphrased
        return render(request, "onboard_review.html", active="onboard",
                      status_code=status_code, view=view, name=name, here="review",
                      groups=grouped(view), dry=dry, refusal=refusal, error=error,
                      reason=view["reason"] if reason is None else reason,
                      prev=view["sections"][-2] if len(view["sections"]) > 1 else None)

    @app.get("/onboard/{name}/review", response_class=HTMLResponse)
    def onboard_review(request: Request, name: str):
        """What applying will change, and what stops it (§5)"""
        return review(request, name)

    @app.post("/onboard/{name}/apply", response_class=HTMLResponse)
    def onboard_apply(request: Request, name: str, rev: str = Form(""),
                      reason: str = Form("")):
        """Save the reason, check rev and must-haves, then `ops.onboard_apply` (§5)"""
        try:
            view = ops.onboard_view(name)
        except ops.OpsError as e:
            return render(request, "error.html", active="onboard", message=str(e),
                          status_code=404)
        try:
            if reason.strip() and reason != view["reason"]:
                view = ops.onboard_edit(name, "reason", rev=rev, value=reason)
            elif rev != view["rev"]:
                raise ops.OpsError(ops.ONBOARD_STALE)
            missing = view["progress"]["must_have_missing"]
            if missing:
                raise ops.OpsError(
                    f"decide first: {', '.join(missing)} — each needs an answer (a posture, "
                    f"accept defaults or keep these) before this draft can be applied")
            result = ops.onboard_apply(view["draft_path"])
        except ops.OpsError as e:
            return review(request, name, error=str(e), reason=reason, status_code=400)
        try:
            view = ops.onboard_view(name)  # the rail as it is now
        except ops.OpsError:
            pass
        return render(request, "onboard_applied.html", active="onboard", view=view,
                      name=name, here="review", result=result,
                      config_url=f"/config?scope={quote('projects.' + name)}#cfg-history")
