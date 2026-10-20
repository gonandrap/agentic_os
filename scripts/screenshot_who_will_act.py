"""Screenshot the "who will act" row of the debug page's diagnosis panel — §8 of
docs/superpowers/specs/2026-10-09-derive-somebody-is-going-to-act-on-this.md.

The row sits right under "waiting on" and names the daemon pass coming for the order
(`pass_name · predicate`) plus the sentence saying why. The fixture is a PENDING order,
which `project_store.claim_sql_selects` selects, so `Daemon.dispatch_pending` is the
actor — the same premise `tests/test_ui_debug.py` §3b asserts in strings.

Everything lives in a temp `JARVIS_HOME`, so it never touches the live OS:

    uv run python scripts/screenshot_who_will_act.py
"""

from __future__ import annotations

import json
import os
import socket
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SHOTS = REPO / "docs" / "screenshots"
OUT = SHOTS / "who-will-act.png"
PORT = 8798


def seed() -> tuple[str, str]:
    """One pending work order in a fresh home. Pending and nothing else: the actor is
    derived from the row's status and the claim SQL, so any further state would only
    risk moving it to a different pass."""
    from jarvis import ops
    from jarvis.central_store import CentralStore

    home = Path(os.environ["JARVIS_HOME"])
    project = home / "jarvis_os"
    project.mkdir(parents=True, exist_ok=True)
    catalog = home / "catalog.json"
    catalog.write_text(json.dumps({
        "os": {},
        "projects": [{"name": "jarvis_os", "path": str(project),
                      "description": "the OS itself"}],
    }))
    central = CentralStore()
    central.upsert_project("jarvis_os", str(project), "the OS itself")
    central.set_state("catalog_path", str(catalog))
    central.conn.commit()
    central.close()

    wo = ops.create_work_order(
        "jarvis_os", "derive who is going to act on this order")
    return "jarvis_os", wo["id"]


def check_actor(project: str, wo_id: str) -> None:
    """The premise, before any browser: this row HAS an actor. A screenshot of the
    fallback sentence would look like a captured feature and prove nothing."""
    from jarvis import ops
    from jarvis.project_store import ProjectStore

    store = ProjectStore(Path(os.environ["JARVIS_HOME"]) / project)
    try:
        actor = ops.scheduled_actor(store, store.get_work_order(wo_id))
    finally:
        store.close()
    if actor is None:
        raise SystemExit("the fixture has no scheduled actor — nothing was captured")
    print(f"actor: {actor.pass_name} · {actor.predicate}")


def serve() -> None:
    import uvicorn

    from jarvis.ui.app import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="warning")


def shoot(project: str, wo_id: str) -> Path:
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(f"http://127.0.0.1:{PORT}/wo/{project}/{wo_id}/debug")
        page.wait_for_selector("#block-diagnosis")
        text = page.locator("#block-diagnosis").inner_text()
        assert "who will act" in text, "the row is not on the page"
        assert "no daemon pass will act on this order" not in text, \
            "the page rendered the fallback — the fixture lost its actor"
        page.locator("#block-diagnosis").screenshot(path=OUT)
        browser.close()
    return OUT


def wait_for_server(timeout: float = 30.0) -> None:
    import urllib.error
    import urllib.request

    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(f"http://127.0.0.1:{PORT}/", timeout=2) as r:
                if r.status == 200:
                    return
        except (urllib.error.URLError, OSError):
            time.sleep(0.2)
    raise SystemExit(f"the dashboard never answered on port {PORT} within {timeout:.0f}s "
                     f"— nothing was captured, so no screenshot is stale")


def main() -> int:
    tmp = tempfile.mkdtemp()
    os.environ["JARVIS_HOME"] = tmp
    os.environ["JARVIS_TRANSCRIPT_ROOT"] = str(Path(tmp) / "claude-projects")
    os.environ.pop("JARVIS_WO_ID", None)
    sys.path.insert(0, str(REPO / "src"))
    project, wo_id = seed()
    check_actor(project, wo_id)
    with socket.socket() as probe:
        if probe.connect_ex(("127.0.0.1", PORT)) == 0:
            raise SystemExit(f"port {PORT} is already answering — another copy of this "
                             f"script is running; nothing was captured")
    threading.Thread(target=serve, daemon=True).start()
    wait_for_server()
    path = shoot(project, wo_id)
    print(f"{path}  {path.stat().st_size:,} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
