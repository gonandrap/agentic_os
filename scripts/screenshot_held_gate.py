"""Screenshot what a work order held on an unargued gate now says about itself.

Two surfaces: `/gates` files an `awaiting_case` row under "Awaiting the worker's case",
and the work-order page. Fix 2 of
docs/superpowers/specs/2026-09-29-a-heredoc-edit-is-not-a-merge.md stopped
`gates.file_request` writing `waiting_input` for a held (`hold=True`) request — the
command stays blocked, but nothing is owed by the user, so the order reads plain Running:
no "Waiting on you" line, no attention flag. `scripts/screenshot_health_surfaces.py` is
the shape this copies.

    uv run python scripts/screenshot_held_gate.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SHOTS = REPO / "docs" / "screenshots"
PORT = 8803


def seed() -> str:
    """One work order parked on a request the worker recorded and never argued."""
    from jarvis import gates, ops
    from jarvis.central_store import CentralStore
    from jarvis.project_store import ProjectStore
    from jarvis.testing import make_git_project

    home = Path(tempfile.mkdtemp())
    project = make_git_project(home, "jarvis_os")
    document = {
        "os": {"defaults": {"model": "opus"}, "ui": {"port": 8787},
               "notifications": {"sinks": ["log"]}},
        "projects": [{"name": "jarvis_os", "path": str(project),
                      "description": "the OS itself",
                      "gates": {"enabled": list(gates.KIND_NAMES),
                                "case_ttl_seconds": 600}}],
    }
    catalog = home / "catalog.json"
    catalog.write_text(json.dumps(document, indent=2))
    store = CentralStore()
    store.set_state("catalog_path", str(catalog))
    store.close()
    ops.start_os(str(catalog), foreground=True)

    wo = ops.create_work_order(
        "jarvis_os", "Cut 0.9.1 with the gate-wording fix",
        description="Release once PR 201 is merged and the suite is green.")
    _, path, _ = ops.find_work_order(wo["id"], "jarvis_os")
    pstore = ProjectStore(path)
    pstore.set_status(wo["id"], "running")
    # The attempt, not the request: the worker ran it rather than asking, so the row is
    # recorded with the placeholder case and in front of nobody (gates.AWAITING_CASE).
    # `hold=True`'s own road (gates.file_request, fix 2): no status write here — the
    # order stays `running`, not `waiting_input`, because nothing is owed by the user.
    pstore.add_approval(wo["id"], kind="release", command="./scripts/" + "shipit.sh",
                        matched="ship" + "it",
                        justification=gates.NO_CASE_JUSTIFICATION,
                        status=gates.AWAITING_CASE)
    pstore.close()
    return wo["id"]


def serve() -> None:
    import uvicorn

    from jarvis.ui.app import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="warning")


def shoot(wo_id: str) -> None:
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 1000})

        page.goto(f"http://127.0.0.1:{PORT}/gates")
        heading = page.get_by_role("heading", name="Awaiting the worker's case")
        head = heading.bounding_box()
        # The panel this heading owns, not "the second panel on the page": which index
        # that is depends on whether anything is escalated.
        panel = heading.locator(
            "xpath=following-sibling::div[contains(@class,'panel')][1]").bounding_box()
        page.screenshot(path=SHOTS / "gates-awaiting-case.png", full_page=True, clip={
            "x": head["x"] - 8, "y": head["y"] - 8,
            "width": max(head["width"], panel["width"]) + 16,
            "height": panel["y"] + panel["height"] - head["y"] + 16})

        page.goto(f"http://127.0.0.1:{PORT}/wo/jarvis_os/{wo_id}")
        card = page.locator("div.panel").first
        text = card.inner_text()
        assert "running" in text, text
        assert "Waiting on you" not in text, text
        card = card.bounding_box()
        page.screenshot(path=SHOTS / "wo-held-gate.png", full_page=True, clip={
            "x": card["x"] - 8, "y": card["y"] - 8,
            "width": card["width"] + 16, "height": card["height"] + 16})
        browser.close()


def main() -> int:
    os.environ["JARVIS_HOME"] = tempfile.mkdtemp()
    os.environ.pop("JARVIS_WO_ID", None)
    sys.path.insert(0, str(REPO / "src"))
    wo_id = seed()
    threading.Thread(target=serve, daemon=True).start()
    time.sleep(2)
    shoot(wo_id)
    print("\n".join(str(SHOTS / f"{n}.png")
                    for n in ("gates-awaiting-case", "wo-held-gate")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
