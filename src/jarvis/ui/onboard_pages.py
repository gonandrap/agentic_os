"""The onboarding workspace: rail, one section per URL, inline edits (docs/superpowers/specs/2026-11-09-onboarding-from-the-dashboard.md §3)"""

from __future__ import annotations

import re
from typing import Any, Callable
from urllib.parse import quote

from fastapi import FastAPI, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from .. import ops


def slug(path: str) -> str:
    """§0.3: the row anchor for a knob path"""
    return re.sub(r"[^A-Za-z0-9_-]", "-", path or "")


def section_url(name: str, key: str, focus: str = "") -> str:
    url = f"/onboard/{quote(name)}/{quote(key)}"
    return f"{url}?focus={quote(focus)}#k-{slug(focus)}" if focus else url


def first_section(view: dict[str, Any]) -> str:
    """First unanswered must-have, else first unanswered area, else review (§0.3)"""
    missing = view["progress"]["must_have_missing"]
    if missing:
        return missing[0]
    return next((s["key"] for s in view["sections"]
                 if s["key"] not in ("identity", "review") and s["status"] == "untouched"),
                "review")


def identity_gaps(view: dict[str, Any]) -> list[str]:
    """The `project.*` fields still empty: what a copy never fills (§1)"""
    section = next(s for s in view["sections"] if s["key"] == "identity")
    return [k["path"] for k in section["knobs"] if k["kind"] == "identity" and not k["value"]]


def _rows(section: dict[str, Any], error_path: str, typed: str | None) -> list[dict[str, Any]]:
    from .app import config_input_text, config_widget

    out = []
    for k in section["knobs"]:
        shown = k["value"] if k["value"] is not None else k["default"]
        row = {**k, "slug": slug(k["path"]), "widget": config_widget(shown),
               "text": config_input_text(k["value"])}
        if typed is not None and k["path"] == error_path:
            row["text"] = typed  # §3: a refused value stays in the editor
        out.append(row)
    return out


def register(app: FastAPI, render: Callable[..., HTMLResponse]) -> None:
    def not_found(request: Request, message: str) -> HTMLResponse:
        return render(request, "error.html", active="onboard", message=message,
                      status_code=404)

    def page(request: Request, name: str, key: str, focus: str = "", *,
             error: str = "", error_path: str = "", typed: str | None = None,
             status_code: int = 200) -> HTMLResponse:
        try:
            view = ops.onboard_view(name)
        except ops.OpsError as e:
            return not_found(request, str(e))
        keys = [s["key"] for s in view["sections"]]
        # `review` belongs to §5's page; the rail only links to it
        if key not in keys or key == "review":
            return not_found(request, (f"{error} — " if error else "")
                             + f"no section {key!r} in the {name} onboarding — "
                               f"sections: {', '.join(keys)}")
        i = keys.index(key)
        section = view["sections"][i]
        rows = _rows(section, error_path, typed)
        on_row = any(r["path"] == error_path for r in rows)
        return render(
            request, "onboard_section.html", active="onboard", status_code=status_code,
            view=view, name=name, here=key, section=section, focus=focus,
            asked=[r for r in rows if r["kind"] != "classified"],
            classified=[r for r in rows if r["kind"] == "classified"],
            prefilled=key != "identity" and section["status"] == "untouched"
            and any(r["source"] in ("draft", "catalog") for r in rows
                    if r["kind"] != "identity"),
            error=error, error_path=error_path if on_row else "",
            prev=view["sections"][i - 1] if i > 0 else None,
            next=view["sections"][i + 1] if i + 1 < len(keys) else None,
            slug=slug)

    @app.get("/onboard/{name}", response_class=HTMLResponse)
    def onboard_draft(request: Request, name: str):
        """Straight to the first section that needs the user (§0.3)"""
        try:
            view = ops.onboard_view(name)
        except ops.OpsError as e:
            return not_found(request, str(e))
        return RedirectResponse(section_url(name, first_section(view)), status_code=303)

    @app.get("/onboard/{name}/{section}", response_class=HTMLResponse)
    def onboard_section(request: Request, name: str, section: str, focus: str = ""):
        """One section of the draft; `focus` marks one row (§3)"""
        return page(request, name, section, focus)

    @app.post("/onboard/{name}/edit", response_class=HTMLResponse)
    def onboard_edit(request: Request, name: str, verb: str = Form(""),
                     section: str = Form(""), rev: str = Form(""), path: str = Form(""),
                     value: str = Form(""), mode: str = Form(""), field: str = Form(""),
                     lever: str = Form(""), wired: str = Form(""), fleet: str = Form("")):
        """`ops.onboard_edit` under another name; a refusal re-renders the section (§3)"""
        target = path or (f"project.{field}" if field else "") or lever
        try:
            view = ops.onboard_edit(name, verb, rev=rev, section=section, path=path,
                                    value=value, mode=mode, field=field, lever=lever,
                                    wired=wired, fleet=fleet)
        except ops.OpsError as e:
            return page(request, name, section, target, error=str(e), error_path=target,
                        typed=value if verb in ("set", "identity") else None,
                        status_code=400)
        if view["project"]["like"]:  # a copied draft is overridden from review (§1)
            return RedirectResponse(f"/onboard/{quote(name)}/review", status_code=303)
        if not section:
            return RedirectResponse(f"/onboard/{quote(name)}", status_code=303)
        url = section_url(name, section)
        return RedirectResponse(f"{url}#k-{slug(target)}" if target else url,
                                status_code=303)
