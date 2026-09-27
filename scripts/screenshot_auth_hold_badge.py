"""Screenshot a round HELD FOR AUTHENTICATION, badge and timeline row, for issue #778.

`scripts/screenshot_ci_hold_badge.py` is the shape this copies. ONE IMAGE carrying BOTH
surfaces, because the claim is that the two agree: the round row reads `held for
authentication` toned `active` rather than a red ✗, and the timeline line says what it is
waiting FOR and names no moment — `reopens_at` on this cause is a recheck interval.

Writes the PNG to docs/screenshots/. Everything it touches lives in a temp `JARVIS_HOME`
and a temp catalog, so it never reads or writes the live OS:

    uv run python scripts/screenshot_auth_hold_badge.py
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
PORT = 8806

PR = "https://github.com/gonandrap/agentic_os/pull/778"
AUTH = "Failed to authenticate: OAuth session expired and could not be refreshed"


def _repo(path: Path) -> None:
    """An empty checkout, the shape `testing.make_git_project` builds for the suite."""
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    (path / "README.md").write_text("# jarvis_os\n")


def order(store) -> str:
    """One work order parked behind a pull request, its round held for authentication.

    THE CLOSE AND THE EVENT GO THROUGH THE REAL WRITE PATH —
    `Daemon._validation_auth_held` itself — so a change that stopped passing `hold_cause`
    or stopped writing `attempt` shows up in the picture instead of being papered over
    here. Only the panel's FAILURE is staged, because a real one needs a spent sign-in.
    """
    from jarvis import ops
    from jarvis.claude_cli import AuthFailure
    from jarvis.daemon import Daemon

    wo = ops.create_work_order(
        "jarvis_os", "The panel must not mistake an auth failure for a verdict",
        "Hold the round when every seat fails to authenticate, instead of escalating it.")
    ops.finish(wo["id"], "opened a pull request", pr_url=PR,
               evidence="uv run pytest tests/ evals/ — 4252 passed")
    rnd = store.latest_validation_round(wo_id=wo["id"]) \
        or store.open_validation_round(wo_id=wo["id"], fingerprint="7b41e0c9d582")
    Daemon._validation_auth_held(store, store.get_work_order(wo["id"]),
                                 int(rnd["id"]), int(rnd["round"]),
                                 AuthFailure(message=AUTH))
    store.set_status(wo["id"], "validating")
    return wo["id"]


def seed() -> tuple[Path, str]:
    from jarvis import ops
    from jarvis.central_store import CentralStore
    from jarvis.project_store import ProjectStore

    home = Path(tempfile.mkdtemp())
    project = home / "jarvis_os"
    project.mkdir(parents=True)
    _repo(project)
    catalog = home / "catalog.json"
    catalog.write_text(json.dumps({
        "os": {"ui": {"port": 8787}},
        "projects": [{"name": "jarvis_os", "path": str(project),
                      "description": "the OS itself",
                      "validation": {"enabled": True, "auto_merge": True}}],
    }, indent=2))
    store = CentralStore()
    store.set_state("catalog_path", str(catalog))
    store.close()
    ops.start_os(str(catalog), foreground=True)

    pstore = ProjectStore(project)
    wo_id = order(pstore)
    pstore.close()
    return catalog, wo_id


def serve() -> None:
    import uvicorn

    from jarvis.ui.app import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="warning")


def shoot(wo_id: str) -> None:
    """One clip spanning the badge and the timeline row, at the page's own scale.

    A full-page shot of this page puts the badge at a few pixels tall, which is no
    evidence of anything; a pair of element shots would not show that the two surfaces
    agree. So the clip is computed from both boxes.
    """
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    out = SHOTS / "validation-auth-hold.png"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1100, "height": 1000},
                                device_scale_factor=2)
        page.goto(f"http://127.0.0.1:{PORT}/wo/jarvis_os/{wo_id}")
        # The timeline lives behind a tab; the round's badge does not. Opening the tab is
        # what puts the two surfaces on one screen.
        page.locator("button[role=tab][data-panel=tab-timeline]").click()
        page.wait_for_timeout(300)
        badge = page.locator("span.st", has_text="held for authentication").first
        row = page.locator("ul.timeline li",
                           has_text="Claude Code could not authenticate").first
        top = min(badge.bounding_box()["y"], row.bounding_box()["y"])
        bottom = max(badge.bounding_box()["y"] + badge.bounding_box()["height"],
                     row.bounding_box()["y"] + row.bounding_box()["height"])
        page.screenshot(path=out, clip={"x": 0, "y": max(top - 48, 0),
                                        "width": 1100,
                                        "height": bottom - top + 96})
        print("badge:", badge.inner_text())
        print("row:", row.inner_text())
        browser.close()
    print(out)


def main() -> int:
    os.environ["JARVIS_HOME"] = tempfile.mkdtemp()
    os.environ.pop("JARVIS_WO_ID", None)
    sys.path.insert(0, str(REPO / "src"))
    _catalog, wo_id = seed()
    threading.Thread(target=serve, daemon=True).start()
    time.sleep(2)
    shoot(wo_id)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
