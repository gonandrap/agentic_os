"""Screenshot the io header's dispatched block and the analyst's sibling list (issue 1323)"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import screenshot_improvement_order as base  # noqa: E402


def seed() -> tuple[str, str]:
    """The base seed, plus a PR on the dispatched order"""
    from jarvis import ops
    from jarvis.project_store import ProjectStore

    reviewed, _ = base.seed()
    io = ops.show_improvement_order(reviewed)
    _n, path, _wo = ops.find_work_order(io["dispatched"]["orders"][0]["id"])
    store = ProjectStore(path)
    try:
        store.update_work_order(io["dispatched"]["orders"][0]["id"],
                                pr_url="https://github.com/gonandrap/agentic_os/pull/1306")
    finally:
        store.close()
    return reviewed, io["analyst"]["id"]


def shoot(io_id: str, analyst_id: str) -> None:
    from playwright.sync_api import sync_playwright

    url = f"http://127.0.0.1:{base.PORT}"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 700})
        page.goto(f"{url}/io/{base.PROJECT}/{io_id}")
        page.screenshot(path=base.SHOTS / "io_dispatched_header.png")
        page.goto(f"{url}/wo/{base.PROJECT}/{analyst_id}")
        page.screenshot(path=base.SHOTS / "io_dispatched_analyst.png")
        browser.close()


def main() -> int:
    home = tempfile.mkdtemp()
    os.environ["JARVIS_HOME"] = home
    os.environ["JARVIS_TRANSCRIPT_ROOT"] = str(Path(home) / "transcripts")
    os.environ["JARVIS_SPEND_HOME"] = home
    os.environ.pop("JARVIS_WO_ID", None)
    sys.path.insert(0, str(base.REPO / "src"))
    io_id, analyst_id = seed()
    threading.Thread(target=base.serve, daemon=True).start()
    time.sleep(2)
    shoot(io_id, analyst_id)
    print(base.SHOTS / "io_dispatched_header.png", base.SHOTS / "io_dispatched_analyst.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
