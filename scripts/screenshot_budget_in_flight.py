"""Screenshot the budget line while a turn is STILL RUNNING, for a PR's UI evidence.

`scripts/screenshot_budget_at_creation.py` is the shape this copies. The line it shoots
is the one issue #471 was filed against: it read `Budget: $0.00 of $30.00` directly
under a bill chip saying `~$3.25`, because a turn writes the CLI's own cost only when it
ends and the recorded total was the only thing on the page.

  budget-in-flight.png    the work order page mid-turn: `~$25.00 of $30.00`, the `~`
                          marking the half of that figure that is a list-price estimate
                          off the live transcript

Writes the PNG to docs/screenshots/. Everything it touches lives in a temp `JARVIS_HOME`,
a temp catalog and a temp transcript root, so it never reads or writes the live OS:

    uv run python scripts/screenshot_budget_in_flight.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SHOTS = REPO / "docs" / "screenshots"
PORT = 8806
SESSION = "sess-in-flight"


def stamp(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat().replace("+00:00", "Z")


def transcript(started_at: float) -> None:
    """One $25 opus call inside the running turn's window."""
    from jarvis import usage

    root = Path(os.environ[usage.TRANSCRIPT_ROOT_ENV]) / "-proj"
    root.mkdir(parents=True, exist_ok=True)
    (root / f"{SESSION}.jsonl").write_text(json.dumps({
        "type": "assistant",
        "timestamp": stamp(started_at + 30),
        "message": {"id": "msg-1", "model": "claude-opus-5", "usage": {
            "input_tokens": 0, "cache_creation_input_tokens": 0,
            "cache_read_input_tokens": 0, "output_tokens": 1_000_000}},
    }) + "\n")


def seed() -> str:
    from jarvis import ops
    from jarvis.central_store import CentralStore
    from jarvis.project_store import ProjectStore

    home = Path(tempfile.mkdtemp())
    project = home / "jarvis_os"
    project.mkdir(parents=True)
    subprocess.run(["git", "init", "-q"], cwd=project, check=True)
    (project / "README.md").write_text("# jarvis_os\n")
    catalog = home / "catalog.json"
    catalog.write_text(json.dumps({
        "os": {"ui": {"port": 8787}},
        "projects": [{"name": "jarvis_os", "path": str(project),
                      "description": "the OS itself"}],
    }, indent=2))
    store = CentralStore()
    store.set_state("catalog_path", str(catalog))
    store.close()
    ops.start_os(str(catalog), foreground=True)

    wo = ops.create_work_order(
        "jarvis_os", "Trace every cache write to its cause",
        description="Label cold-start, ttl-expiry and prefix-miss apart.",
        budget_usd=30.0)
    s = ProjectStore(project)  # the store is keyed by the project's PATH
    s.update_work_order(wo["id"], session_id=SESSION, status="running")
    turn = s.create_turn(wo["id"], kind="dispatch", prompt="work")
    started = time.time() - 60
    s.conn.execute("UPDATE wo_turns SET started_at=? WHERE id=?", (started, turn["id"]))
    s.close()
    transcript(started)
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
        page = browser.new_page(viewport={"width": 1280, "height": 700})
        page.goto(f"http://127.0.0.1:{PORT}/wo/jarvis_os/{wo_id}")
        page.wait_for_timeout(300)
        page.screenshot(path=SHOTS / "budget-in-flight.png")
        browser.close()


def main() -> int:
    os.environ["JARVIS_HOME"] = tempfile.mkdtemp()
    os.environ.pop("JARVIS_WO_ID", None)
    sys.path.insert(0, str(REPO / "src"))
    from jarvis import usage

    os.environ[usage.TRANSCRIPT_ROOT_ENV] = tempfile.mkdtemp()
    wo_id = seed()
    threading.Thread(target=serve, daemon=True).start()
    time.sleep(2)
    shoot(wo_id)
    print(SHOTS / "budget-in-flight.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
