"""Screenshot the work-order page's pull-request state line, for a PR's UI evidence.

TWO states, because one picture of a line that renders proves nothing about the
derivation: an order with a `pr_merged` event, which reads `⑃ state: MERGED at <sha10>`
under the link, and an order carrying the same pull request with NO such event, which
must show the link and NO state line at all. An empty state means nothing has looked
yet, never "did not merge" (`ops.merge_state`, spec
docs/superpowers/specs/2026-09-25-a-gate-records-the-pull-request.md §7).

Writes PNGs to docs/screenshots/. Everything it touches lives in a temp `JARVIS_HOME`
and a temp catalog, so it never reads or writes the live OS:

    uv run python scripts/screenshot_pr_merge_state.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SHOTS = REPO / "docs" / "screenshots"
PORT = 8804

PR = "https://github.com/gonandrap/agentic_os/pull/735"
HEAD = "9f3c1ad20b7e4856c9d1f0a2b3c4d5e6f7089aab"


def _repo(path: Path) -> None:
    """An empty checkout, the shape `testing.make_git_project` builds for the suite."""
    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    (path / "README.md").write_text("# jarvis_os\n")


def order(store, title: str, *, merged: bool) -> str:
    """One order parked behind a pull request, with or without a landing observed."""
    from jarvis import ops

    wo = ops.create_work_order("jarvis_os", title,
                               "Record the pull request an approved merge gate names, "
                               "so an order that settled without `wo finish --pr` "
                               "still has the link on its record.")
    ops.finish(wo["id"], "opened a pull request", pr_url=PR)
    if merged:
        # The event `Daemon.refresh_landings` writes when GitHub says it landed — the
        # only source of MERGED, never the `pr_state` column (kn-dbc4971d).
        store.add_event(wo["id"], "pr_merged", {
            "pr_url": PR, "head_oid": HEAD, "source": "landing_sweep",
            "merged_at": "2026-09-25T21:14:07Z"})
        store.set_status(wo["id"], "completed")
    return wo["id"]


def seed() -> list[str]:
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
                      # Off, so `finish` settles straight to `waiting_pr_merge`: a round
                      # in flight would put this page's validation section in the shot
                      # and say nothing about the line being demonstrated.
                      "validation": {"enabled": False}}],
    }, indent=2))
    store = CentralStore()
    store.set_state("catalog_path", str(catalog))
    store.close()
    ops.start_os(str(catalog), foreground=True)

    pstore = ProjectStore(project)
    ids = [
        order(pstore, "An approved merge gate records the pull request", merged=True),
        order(pstore, "Bind a planner's settlement to the pull request it opened",
              merged=False),
    ]
    pstore.close()
    return ids


def serve() -> None:
    import uvicorn

    from jarvis.ui.app import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="warning")


def shoot(ids: list[str]) -> None:
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    base = f"http://127.0.0.1:{PORT}"
    names = ["pr-merge-state-merged", "pr-merge-state-unlooked"]
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 760})
        for name, wo_id in zip(names, ids):
            page.goto(f"{base}/wo/jarvis_os/{wo_id}")
            page.wait_for_selector("p.kv:has-text('pull request')")
            body = page.inner_text("body")
            assert PR in body, f"{name}: no pull request link"
            expected = f"state: MERGED at {HEAD[:10]}"
            if name.endswith("merged"):
                assert expected in body, f"{name}: missing {expected!r}"
            else:
                assert "state:" not in body, f"{name}: a state line with nothing looked"
            page.screenshot(path=SHOTS / f"{name}.png")
        browser.close()


def main() -> int:
    os.environ["JARVIS_HOME"] = tempfile.mkdtemp()
    os.environ.pop("JARVIS_WO_ID", None)
    sys.path.insert(0, str(REPO / "src"))
    ids = seed()
    threading.Thread(target=serve, daemon=True).start()
    time.sleep(2)
    shoot(ids)
    print("\n".join(str(p) for p in sorted(SHOTS.glob("pr-merge-state-*.png"))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
