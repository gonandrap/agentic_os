"""Screenshot /release and one release against a throwaway home and repo, for a PR's UI evidence"""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SHOTS = REPO / "docs" / "screenshots"
PORT = 8799


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


def seed() -> Path:
    from jarvis import morpheus, ops
    from jarvis.catalog import MorpheusConfig
    from jarvis.central_store import CentralStore
    from jarvis.project_store import ProjectStore

    repo = Path(tempfile.mkdtemp()) / "jarvis_os"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    central = CentralStore()
    central.upsert_project("jarvis_os", str(repo), "the OS")
    central.conn.commit()
    store = ProjectStore(repo)
    io = store.create_feature_order(title="gates stall", kind="improvement",
                                    metadata={ops.EVIDENCE_REFS_KEY: ["inv-0000beef"]})
    fo = store.create_feature_order(title="decision orders", kind="feature")
    plan = [("fix the gate stall", io["id"], "jarvis-0.1.0"), ("decision order form", fo["id"], None),
            ("decision order search", fo["id"], "jarvis-0.1.1"),
            ("release console", None, None), ("steward page", None, None)]
    states = ["validated", "failed", "monitoring", None, None]
    for n, ((title, parent, tag), state) in enumerate(zip(plan, states), start=1):
        wo = store.create_work_order(title, parent_id=parent)
        _git(repo, "commit", "-q", "--allow-empty", "-m", f"[{wo['id']}] {title} (#{n})")
        if state:
            item = morpheus.enqueue(central, MorpheusConfig(), morpheus.PullRequestBody(
                number=n, url=f"https://github.com/o/r/pull/{n}", title=title,
                body="## How to validate in production\n\nlook\n"), project="jarvis_os",
                repo="o/r")
            central.update_morpheus_item(item["id"], state=state)
        if tag:
            _git(repo, "tag", tag)
    store.close()
    central.close()
    return repo


def serve(repo: Path) -> None:
    import uvicorn

    from jarvis import ops
    from jarvis.ui.app import create_app

    ops._release_repo = lambda: ("jarvis_os", repo)
    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="warning")


def shoot() -> None:
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(f"http://127.0.0.1:{PORT}/release")
        page.locator('input[type=radio]').nth(1).check()
        page.screenshot(path=SHOTS / "release-console.png", full_page=True)
        page.goto(f"http://127.0.0.1:{PORT}/release/jarvis-0.1.1")
        page.screenshot(path=SHOTS / "release-detail.png", full_page=True)
        browser.close()


def main() -> int:
    os.environ["JARVIS_HOME"] = tempfile.mkdtemp()
    sys.path.insert(0, str(REPO / "src"))
    repo = seed()
    threading.Thread(target=serve, args=(repo,), daemon=True).start()
    time.sleep(2)
    shoot()
    print("\n".join(str(p) for p in sorted(SHOTS.glob("release-*.png"))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
