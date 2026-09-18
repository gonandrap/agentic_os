"""Screenshot the RE-JUDGE control on the work-order page, for a PR's UI evidence.

`scripts/screenshot_forced_round.py` is the shape this copies, and the two are about
different halves: that one is about how a forced ROUND reads afterwards, this one is
about the control that opens it and the diagnosis beside it.

Three shots, because the claim has three parts and a shot of the button alone shows
none of them: the diagnosis on a pull request whose head moved out from under the pass,
the two lines the page reports after the button is actually pressed, and the control
DISABLED on an order that may not be re-judged with the rule visible beside it.

Writes PNGs to docs/screenshots/. Everything it touches lives in a temp `JARVIS_HOME`
and a temp catalog, so it never reads or writes the live OS:

    uv run python scripts/screenshot_rejudge_control.py
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
PORT = 8803

PR = "https://github.com/gonandrap/agentic_os/pull/243"
#: The real pair from wo-752eced8: the panel passed the first, then `origin/main` was
#: merged into the branch to clear a conflict and the head became the second.
JUDGED = "9ee728c739a1b2c3d4e5f60718293a4b5c6d7e8f"
HEAD = "3a5ef7a343b2c1d0e9f8a7b6c5d4e3f2a1b09876"


def _repo(path: Path) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    (path / "README.md").write_text("# jarvis_os\n")


def parked(store) -> str:
    """A work order parked behind a pull request whose head has MOVED since the pass.

    The hold is written by `Daemon._note_automerge_held`'s own payload shape rather than
    invented here — the page reads that event and nothing else, so a shot built from a
    different shape would be a picture of a surface nobody has.
    """
    from jarvis import ops

    wo = ops.create_work_order(
        "jarvis_os", "The OS may write to a second kind of repository",
        "Take the repository name from the catalog rather than from the remote.")
    ops.finish(wo["id"], "opened a pull request", pr_url=PR,
               evidence="uv run pytest tests/ evals/ — 3812 passed")
    rnd = store.latest_validation_round(wo_id=wo["id"])
    store.set_validation_head(rnd["id"], JUDGED)
    store.close_validation_round(rnd["id"], "passed", "")
    store.record_validation_opinion(rnd["id"], "tester", verdict="pass",
                                    reply="The name resolves from the catalog.",
                                    model="claude-opus-5", latency_ms=18400)
    store.set_status(wo["id"], "waiting_pr_merge")
    store.add_event(wo["id"], "automerge_held", {
        "code": "sha_moved", "round": 1, "judged_sha": JUDGED, "head_sha": HEAD,
        "reason": f"round 1 passed on {JUDGED[:10]}, the head is now {HEAD[:10]}"})
    return wo["id"]


def settled(store) -> str:
    """...and one the verb refuses, so the disabled control has something to explain."""
    from jarvis import ops

    wo = ops.create_work_order("jarvis_os", "A negative slice is not a privileged action",
                               "Teach the gate recogniser that `log[-20:]` ships nothing.")
    ops.finish(wo["id"], "opened a pull request", pr_url=PR, evidence="the suite is green")
    rnd = store.latest_validation_round(wo_id=wo["id"])
    store.close_validation_round(rnd["id"], "passed", "")
    store.set_status(wo["id"], "completed")
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
                      "description": "the OS itself",
                      "validation": {"enabled": True, "auto_merge": True}}],
    }, indent=2))
    store = CentralStore()
    store.set_state("catalog_path", str(catalog))
    store.close()
    ops.start_os(str(catalog), foreground=True)

    pstore = ProjectStore(project)
    held, closed = parked(pstore), settled(pstore)
    pstore.close()
    return catalog, held, closed


def serve() -> None:
    import uvicorn

    from jarvis.ui.app import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="warning")


def shoot(held: str, closed: str) -> None:
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    base = f"http://127.0.0.1:{PORT}"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})

        page.goto(f"{base}/wo/jarvis_os/{held}#rejudge")
        page.wait_for_timeout(200)
        page.screenshot(path=SHOTS / "rejudge-control.png")

        # PRESSED FOR REAL, so the report in the shot is what `ops.force_validation`
        # actually produced rather than a mock-up of it.
        page.fill("textarea[name=reason]",
                  "origin/main was merged in to clear a conflict, so the pass is bound "
                  "to a commit that is no longer the head")
        page.get_by_role("button", name="Open a new round").click()
        page.wait_for_load_state()
        page.wait_for_timeout(200)
        page.screenshot(path=SHOTS / "rejudge-reported.png")

        page.goto(f"{base}/wo/jarvis_os/{closed}#rejudge")
        page.wait_for_timeout(200)
        page.screenshot(path=SHOTS / "rejudge-refused.png")
        browser.close()


def main() -> int:
    os.environ["JARVIS_HOME"] = tempfile.mkdtemp()
    os.environ.pop("JARVIS_WO_ID", None)
    sys.path.insert(0, str(REPO / "src"))
    _catalog, held, closed = seed()
    threading.Thread(target=serve, daemon=True).start()
    time.sleep(2)
    shoot(held, closed)
    print("\n".join(str(q) for q in sorted(SHOTS.glob("rejudge-*.png"))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
