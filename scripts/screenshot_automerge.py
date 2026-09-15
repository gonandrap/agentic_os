"""Screenshot the work-order page's automatic-merge line, for a PR's UI evidence.

Three states on one page, because the line is the whole of what the user sees of this
feature and "the tests passed" says nothing about what it reads like: a merge the OS
performed, a merge it is waiting for permission to perform, and one it is holding
because the head moved after the panel accepted a commit.

Writes PNGs to docs/screenshots/. Everything it touches lives in a temp `JARVIS_HOME`
and a temp catalog, so it never reads or writes the live OS:

    uv run python scripts/screenshot_automerge.py
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
PORT = 8801

PR = "https://github.com/gonandrap/agentic_os/pull/241"
JUDGED = "a1b2c3d4e5f60718293a4b5c6d7e8f9012345678"
PUSHED = "e4f5a6b7c8d90112233445566778899aabbccdde"


def _repo(path: Path) -> None:
    """An empty checkout, the shape `testing.make_git_project` builds for the suite."""
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    (path / "README.md").write_text("# jarvis_os\n")


def order(store, title: str, *, judged: str, events: list[tuple[str, dict]]) -> str:
    """One finished work order parked behind a pull request, with a settled round."""
    from jarvis import ops

    wo = ops.create_work_order("jarvis_os", title,
                               "Give Jarvis the authority to merge a pull request "
                               "itself, but only once the validation panel has "
                               "accepted that exact diff.")
    ops.finish(wo["id"], "opened a pull request", pr_url=PR)
    # The round `finish` already opened, settled — not a second one beside it: two rows
    # would put a permanently `pending` round on the page and make the shot a picture of
    # a bug rather than of the line being demonstrated.
    round_row = store.latest_validation_round(wo_id=wo["id"]) \
        or store.open_validation_round(wo_id=wo["id"], fingerprint="7c1f9a2e04b3")
    store.set_validation_head(round_row["id"], judged)
    store.close_validation_round(round_row["id"], "passed", "")
    store.record_validation_opinion(round_row["id"], "security", verdict="pass",
                                    reply="No new authority reaches a worker.",
                                    model="claude-opus-5", latency_ms=18400)
    store.record_validation_opinion(round_row["id"], "tester", verdict="pass",
                                    reply="The push-after-acceptance case is pinned.",
                                    model="claude-opus-5", latency_ms=22100)
    for kind, payload in events:
        store.add_event(wo["id"], kind, payload)
    # Where the poll finds it: parked behind the pull request, or completed once the OS
    # has merged it. `ops.finish` left it `validating` while the round was open.
    store.set_status(wo["id"],
                     "completed" if events[0][0] == "automerge_merged"
                     else "waiting_pr_merge")
    return wo["id"]


def seed() -> tuple[Path, list[str]]:
    from jarvis import ops
    from jarvis.central_store import CentralStore
    from jarvis.project_store import ProjectStore

    home = Path(tempfile.mkdtemp())
    project = home / "jarvis_os"
    project.mkdir(parents=True)
    # A real repository, because `ops.start_os` registers a project by adopting its
    # checkout — the same thing `testing.make_git_project` does for the suite.
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
    ids = [
        order(pstore, "Merge a validated pull request automatically", judged=JUDGED,
              events=[("automerge_proposed", {
                  "approval_id": 41, "neo_question_id": 88, "round_id": 1, "round": 1,
                  "head_sha": JUDGED, "pr_url": PR})]),
        order(pstore, "Record which commit a validation round judged", judged=JUDGED,
              events=[("automerge_held", {
                  "code": "sha_moved", "round": 1, "judged_sha": JUDGED,
                  "head_sha": PUSHED,
                  "reason": f"round 1 passed on {JUDGED[:10]}, the head is now "
                            f"{PUSHED[:10]}"})]),
        order(pstore, "Bind the merge to the commit the seats read", judged=JUDGED,
              events=[("automerge_merged", {
                  "approval_id": 39, "round_id": 1, "round": 1, "head_sha": JUDGED,
                  "pr_url": PR})]),
    ]
    pstore.close()
    return catalog, ids


def serve() -> None:
    import uvicorn

    from jarvis.ui.app import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="warning")


def shoot(ids: list[str]) -> None:
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    base = f"http://127.0.0.1:{PORT}"
    names = ["automerge-waiting", "automerge-held", "automerge-merged"]
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        for name, wo_id in zip(names, ids):
            page.goto(f"{base}/wo/jarvis_os/{wo_id}")
            page.screenshot(path=SHOTS / f"{name}.png")
        # The commit on the round itself, which is the other surface this touches.
        page.goto(f"{base}/wo/jarvis_os/{ids[1]}")
        page.locator("h2", has_text="Validation").first.scroll_into_view_if_needed()
        page.wait_for_timeout(200)
        page.screenshot(path=SHOTS / "automerge-round-commit.png")
        browser.close()


def main() -> int:
    os.environ["JARVIS_HOME"] = tempfile.mkdtemp()
    os.environ.pop("JARVIS_WO_ID", None)
    sys.path.insert(0, str(REPO / "src"))
    _catalog, ids = seed()
    threading.Thread(target=serve, daemon=True).start()
    time.sleep(2)
    shoot(ids)
    print("\n".join(str(p) for p in sorted(SHOTS.glob("automerge-*.png"))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
