"""Screenshot the harvest block on a work-order page, for a PR's UI evidence.

Issue #888, spec docs/specs/2026-09-30-harvesting-a-dead-turn.md §5: what the OS read
off disk when a turn died renders directly ABOVE the retry control, because it is what
the user reads before pressing the button. This shot is that adjacency.

Everything it touches lives in a temp `JARVIS_HOME` and a temp catalog, so it never
reads or writes the live OS:

    uv run python scripts/screenshot_harvest.py
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
SHOT = "harvest-work-order-page.png"
PORT = 8798
PROJECT = "jarvis_os"

SAID = ("Exporter written and the BibTeX path is green. The CSV writer still drops the "
        "DOI column — I was partway through `csv_writer.py` when this turn ended.")


def _git(cwd: Path, *args: str) -> str:
    env = {"HOME": str(cwd), "PATH": os.environ.get("PATH", ""),
           "GIT_AUTHOR_NAME": "worker", "GIT_AUTHOR_EMAIL": "worker@localhost",
           "GIT_COMMITTER_NAME": "worker", "GIT_COMMITTER_EMAIL": "worker@localhost",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, env=env,
                          capture_output=True, text=True).stdout


def _repo(root: Path) -> Path:
    """A project repository on `main`, with a bare origin so an upstream exists."""
    path = root / PROJECT
    path.mkdir(parents=True)
    _git(path.parent, "init", "-q", "-b", "main", str(path))
    (path / "README.md").write_text("# the citation exporter\n")
    _git(path, "add", "-A")
    _git(path, "commit", "-qm", "base")
    origin = root / "origin.git"
    _git(root, "init", "--bare", "-q", str(origin))
    _git(path, "remote", "add", "origin", str(origin))
    _git(path, "push", "-q", "-u", "origin", "main")
    return path


def seed(project: Path) -> tuple[str, str]:
    """A failed work order whose last turn was harvested — the rich case.

    Three commits on the branch (one of them made in the dead turn), one pushed so the
    tail is unpushed, an uncommitted file for the checkpoint to catch, a last message,
    and a background job the turn left running. Driven through `harvest.write` itself
    rather than through a worker turn: the transport is not what this shot is about.
    """
    from jarvis import background, harvest, ops
    from jarvis.project_store import ProjectStore

    wo = ops.create_work_order(PROJECT, "Export citations as CSV as well as BibTeX")
    wo_id = wo["id"]
    worktree = project / ".claude" / "worktrees" / wo_id
    _git(project, "worktree", "add", "-q", "-b", wo_id, str(worktree), "main")

    for n, name in enumerate(("exporter", "bibtex"), start=1):
        (worktree / f"{name}.py").write_text(f"def {name}(rows):\n    return rows  # {n}\n")
        _git(worktree, "add", "-A")
        _git(worktree, "commit", "-qm", f"add the {name} writer")
    _git(worktree, "push", "-q", "-u", "origin", wo_id)
    head_at_launch = _git(worktree, "rev-parse", "HEAD").strip()

    # …and then the turn that died: one commit it did make, one file it never staged.
    (worktree / "csv_writer.py").write_text("def csv_writer(rows):\n    return rows\n")
    _git(worktree, "add", "-A")
    _git(worktree, "commit", "-qm", "start the CSV writer")
    (worktree / "csv_columns.py").write_text(
        "COLUMNS = (\"title\", \"author\", \"year\")  # the DOI column is still missing\n")

    store = ProjectStore(project)
    store.update_work_order(wo_id, worktree=wo_id, session_id="sess-screenshot")
    turn = store.create_turn(wo_id, kind="message", prompt="carry on with the CSV path")
    store.add_event(wo_id, "turn_started",
                    {"seq": turn["seq"], "kind": "message", "head": head_at_launch})
    store.add_event(wo_id, "hook:Stop", {"last_assistant_message": SAID})
    store.finish_turn(turn["id"], "failed",
                      error="the turn's process ended without writing a result")

    # The one writer of `background_orphaned` still writes it; the transcript this
    # script has no worker to produce is what is stood in for here.
    job = background.Job("b51fl7bhe", "uv run pytest tests/ -q")
    background.orphaned_in_turn = lambda *a, **k: [job]  # type: ignore[assignment]

    harvest.write(store, store.get_work_order(wo_id), store.latest_turn(wo_id), said=SAID)
    store.set_status(wo_id, "failed")
    store.flag_attention(wo_id, "worker turn failed — review and retry")
    store.close()
    return PROJECT, wo_id


def serve() -> None:
    import uvicorn

    from jarvis.ui.app import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="warning")


def shoot(name: str, wo_id: str) -> Path:
    """The harvest section and the retry control below it, in one frame."""
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    out = SHOTS / SHOT
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 1100})
        page.goto(f"http://127.0.0.1:{PORT}/wo/{name}/{wo_id}")
        page.wait_for_load_state()
        harvest = page.locator("h2#harvest")
        harvest.scroll_into_view_if_needed()
        page.wait_for_timeout(200)
        top = harvest.bounding_box()
        # The retry panel is the point of the shot: the clip runs to the bottom of the
        # control, so the two are provably adjacent rather than two separate crops.
        button = page.locator("button", has_text="Retry this order").first
        button.scroll_into_view_if_needed()
        page.wait_for_timeout(200)
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(200)
        top = harvest.bounding_box()
        bottom = button.bounding_box()
        assert top and bottom, "the harvest heading or the retry button did not render"
        page.screenshot(path=out, clip={
            "x": 0, "y": max(top["y"] - 16, 0), "width": 1280,
            "height": (bottom["y"] + bottom["height"] + 24) - max(top["y"] - 16, 0)})
        browser.close()
    return out


def main() -> int:
    home = Path(tempfile.mkdtemp())
    os.environ["JARVIS_HOME"] = str(home / "state")
    os.environ.pop("JARVIS_WO_ID", None)
    sys.path.insert(0, str(REPO / "src"))

    from jarvis import ops

    project = _repo(home / "fleet")
    catalog = home / "catalog.json"
    catalog.write_text(json.dumps({
        "os": {"defaults": {"model": "sonnet"}, "notifications": {"sinks": ["log"]}},
        "projects": [{"name": PROJECT, "path": str(project),
                      "description": "the citation exporter"}],
    }, indent=2))
    ops.start_os(str(catalog), foreground=True)
    name, wo_id = seed(project)

    threading.Thread(target=serve, daemon=True).start()
    time.sleep(2)
    out = shoot(name, wo_id)
    print(f"{out} ({out.stat().st_size} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
