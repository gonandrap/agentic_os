"""Screenshot the RETRY control on the work-order page, for a PR's UI evidence.

`scripts/screenshot_rejudge_control.py` is the shape this copies. Two shots, because
the control has two readings and neither implies the other: a `failed` order that CAN
be retried — enabled box, enabled button, and the line saying a retry buries none of
its pending assumptions — and a `failed` order that cannot, where the refusal sentence
sits directly above the box it disables.

Writes PNGs to docs/screenshots/. Everything it touches lives in a temp `JARVIS_HOME`
and a temp catalog, so it never reads or writes the live OS:

    uv run python scripts/screenshot_retry_control.py
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
PORT = 8804


def _repo(path: Path) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    (path / "README.md").write_text("# jarvis_os\n")


def _failed(store, title: str, description: str, *, session: bool) -> str:
    """A work order whose worker died without delivering — `tests/test_wo_retry.py`'s
    `a_failed_order` shape, minus the dispatch: `failed` plus the attention flag, and
    a session id only where the worker got as far as opening a conversation."""
    from jarvis import ops
    from jarvis import worker_session

    wo = ops.create_work_order("jarvis_os", title, description)
    if session:
        store.update_work_order(wo["id"], session_id=worker_session.new_session_id())
    store.set_status(wo["id"], "failed")
    store.flag_attention(wo["id"], "worker failed — review and retry")
    return wo["id"]


def seed() -> tuple[Path, str, str]:
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
                      "description": "the OS itself"}],
    }, indent=2))
    store = CentralStore()
    store.set_state("catalog_path", str(catalog))
    store.close()
    ops.start_os(str(catalog), foreground=True)

    pstore = ProjectStore(project)
    try:
        live = _failed(
            pstore,
            "The exporter drops a span whose turn is still open",
            "Close the open span at read time rather than skipping the row.",
            session=True)
        never = _failed(
            pstore,
            "A catalog with two projects at one path boots with no error",
            "Refuse the duplicate path at load, naming both projects.",
            session=False)
    finally:
        pstore.close()
    # The pending-assumption line is about a decision the user still owes, so the
    # retriable order gets a real one rather than a stub row.
    ops.assume(live, "Assumed an open span should be closed at its last event rather "
                     "than at read time, so a re-read cannot move it.")
    return catalog, live, never


def serve() -> None:
    import uvicorn

    from jarvis.ui.app import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="warning")


def shoot(live: str, never: str) -> None:
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    base = f"http://127.0.0.1:{PORT}"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 760})

        for wo_id, name in ((live, "wo-retry-control.png"),
                            (never, "wo-retry-refused.png")):
            page.goto(f"{base}/wo/jarvis_os/{wo_id}#retry")
            page.locator("h2#retry").scroll_into_view_if_needed()
            # The heading lands at the top edge on a scroll-into-view; back off so the
            # panel under it is in frame too.
            page.evaluate("window.scrollBy(0, -60)")
            page.wait_for_timeout(300)
            page.screenshot(path=SHOTS / name)
        browser.close()


def main() -> int:
    os.environ["JARVIS_HOME"] = tempfile.mkdtemp()
    os.environ.pop("JARVIS_WO_ID", None)
    sys.path.insert(0, str(REPO / "src"))
    _catalog, live, never = seed()
    threading.Thread(target=serve, daemon=True).start()
    time.sleep(2)
    shoot(live, never)
    print("\n".join(str(q) for q in sorted(SHOTS.glob("wo-retry-*.png"))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
