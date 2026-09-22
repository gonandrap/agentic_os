"""Screenshot a round HELD FOR CI beside one that genuinely failed, for issue #581.

`scripts/screenshot_forced_round.py` is the shape this copies. Two rounds on one work
order, because the pair is the whole claim: both are stored `outcome='failed'`, and only
`hold_cause` says that the first is waiting for GitHub and the second is a reviewer the
OS could not reach. A shot of the waiting badge alone would not show what it is different
FROM — nor that the red ✗ still means what it always meant.

Writes PNGs to docs/screenshots/. Everything it touches lives in a temp `JARVIS_HOME` and
a temp catalog, so it never reads or writes the live OS:

    uv run python scripts/screenshot_ci_hold_badge.py
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

PR = "https://github.com/gonandrap/agentic_os/pull/581"
WAITING = ("waiting for GitHub to finish the checks on this pull request before "
           "judging it: unit (3.13), evals")
OUTAGE = "the validator could not be reached: claude exited 1 after 0 tokens"


def _repo(path: Path) -> None:
    """An empty checkout, the shape `testing.make_git_project` builds for the suite."""
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    (path / "README.md").write_text("# jarvis_os\n")


def order(store) -> str:
    """One work order parked behind a pull request, with the pair of rounds.

    THE CLOSES GO THROUGH `close_validation_round`, not a hand-written UPDATE: the badge
    is evidence that the real write path records the cause, so a change that stopped
    passing `hold_cause` shows up in the picture instead of being papered over here. Only
    the panel's VERDICT is faked, because a real one would need five model calls.
    """
    from jarvis import ops
    from jarvis.project_store import VALIDATION_CI_CAUSE

    wo = ops.create_work_order(
        "jarvis_os", "A round held for CI is not a failed review",
        "Give the round row the cause the timeline event already had.")
    ops.finish(wo["id"], "opened a pull request", pr_url=PR,
               evidence="uv run pytest tests/ evals/ — 4252 passed")
    first = store.latest_validation_round(wo_id=wo["id"]) \
        or store.open_validation_round(wo_id=wo["id"], fingerprint="9d3c1b7fa204")
    store.close_validation_round(first["id"], "failed", WAITING,
                                 hold_cause=VALIDATION_CI_CAUSE)
    second = store.open_validation_round(wo_id=wo["id"], fingerprint="9d3c1b7fa204")
    store.close_validation_round(second["id"], "failed", OUTAGE)
    store.set_status(wo["id"], "waiting_pr_merge")
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
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(f"http://127.0.0.1:{PORT}/wo/jarvis_os/{wo_id}")
        page.locator("h2", has_text="Validation").first.scroll_into_view_if_needed()
        page.wait_for_timeout(200)
        page.screenshot(path=SHOTS / "ci-hold-badge.png")
        browser.close()


def main() -> int:
    os.environ["JARVIS_HOME"] = tempfile.mkdtemp()
    os.environ.pop("JARVIS_WO_ID", None)
    sys.path.insert(0, str(REPO / "src"))
    _catalog, wo_id = seed()
    threading.Thread(target=serve, daemon=True).start()
    time.sleep(2)
    shoot(wo_id)
    print(SHOTS / "ci-hold-badge.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
