"""Screenshot /gates with a contested match and an abandoned request, for PR evidence.

Writes PNGs to docs/screenshots/. Everything it touches lives in a temp `JARVIS_HOME`
and a temp project, so it never reads or writes the live OS:

    uv run python scripts/screenshot_contested_gate.py

See docs/superpowers/specs/2026-09-12-contesting-a-gate-match.md.
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

#: A heredoc whose body quotes the release script — wo-5efc2de6's gate 97. Nothing here
#: ships: the literal is prose in a commit message.
PROSE = ("git commit -F - <<'MSG'\n"
         "Stop the release script ./scripts/shipit.sh clobbering the tag\n"
         "MSG")


def seed() -> None:
    """One project with the four gate outcomes the page has to tell apart: a contested
    match waiting on the user, an abandoned request, a dismissal and a real approval."""
    from jarvis import gates, ops
    from jarvis.central_store import CentralStore
    from jarvis.project_store import ProjectStore

    home = Path(os.environ["JARVIS_HOME"])
    project = home / "jarvis_os"
    (project / ".jarvis").mkdir(parents=True)
    document = {
        "os": {"defaults": {"model": "opus"}, "ui": {"port": 8787},
               "notifications": {"sinks": ["log"]}},
        "projects": [{"name": "jarvis_os", "path": str(project),
                      "description": "the OS itself",
                      "gates": {"enabled": list(gates.KIND_NAMES)}}],
    }
    catalog = home / "catalog.json"
    catalog.write_text(json.dumps(document, indent=2))
    central = CentralStore()
    central.set_state("catalog_path", str(catalog))
    # `ops.list_gates` walks the REGISTERED projects, not the catalog file — a fleet the
    # page can read is one `jarvis start` has bootstrapped.
    central.upsert_project(name="jarvis_os", path=str(project),
                           description="the OS itself",
                           catalog_json=json.dumps(document["projects"][0]))
    central.close()

    store = ProjectStore(project)
    config = gates.GateConfig(enabled=frozenset(gates.KIND_NAMES))

    def filed(title: str, command: str, *, contested: bool = False) -> dict:
        wo = store.create_work_order(title=title, description="seeded for a screenshot")
        action = gates.classify(command, config)
        assert action is not None, f"{command!r} trips no gate — bad fixture"
        return store.add_approval(wo["id"], action.kind, action.command,
                                  matched=action.matched, contested=contested,
                                  justification=(
                                      "the script name is inside the commit message "
                                      "body — this commits, it does not ship"
                                      if contested else
                                      "PR #61 is merged and CI is green on a9f3c21"),
                                  status="awaiting_case" if not contested else "pending")

    contested = filed("Fix the recogniser's heredoc handling", PROSE, contested=True)
    store.mark_approval_escalated(
        contested["id"], "the worker's reading looks right but I cannot verify the "
                         "heredoc is never piped to a shell")

    abandoned = filed("Ship 0.5.4", "./scripts/shipit.sh --stage 0.5.4")
    store.abandon_approval(abandoned["id"],
                           "no case was made for it within 10 minutes, and the match "
                           "was never contested. Nobody reviewed it.")

    dismissed = filed("Run the release staging tests",
                      "uv run pytest tests/test_release_staging.py -k shipit")
    gates.apply_decision(store, dismissed["id"], verdict="dismissed",
                         reason="the literal is a -k test selector; this runs a test",
                         decided_by="neo", project="jarvis_os")

    approved = filed("Merge the gate work", "gh pr merge 61 --squash")
    gates.apply_decision(store, approved["id"], verdict="approved",
                         reason="PR #61 is the work order's own branch, checks green",
                         decided_by="neo", project="jarvis_os")
    store.close()
    del ops  # imported for its side-effect-free path resolution only


def serve() -> None:
    import uvicorn

    from jarvis.ui.app import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="warning")


def shoot() -> None:
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    base = f"http://127.0.0.1:{PORT}"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 1100})
        # The escalated contest: the reviewer is told what is being claimed, and the
        # Approve button is gone — an approval is the one verdict a contest cannot take.
        page.goto(f"{base}/gates")
        page.screenshot(path=SHOTS / "gates-contested.png")

        # The abandoned row and the count beside the rate are the LAST things on the
        # page, so `scroll_into_view_if_needed` stops with them at the bottom edge.
        page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
        page.wait_for_timeout(200)
        page.screenshot(path=SHOTS / "gates-abandoned.png")
        browser.close()


def main() -> int:
    os.environ["JARVIS_HOME"] = tempfile.mkdtemp()
    os.environ.pop("JARVIS_WO_ID", None)
    sys.path.insert(0, str(REPO / "src"))
    seed()
    threading.Thread(target=serve, daemon=True).start()
    time.sleep(2)
    shoot()
    print("\n".join(str(p) for p in sorted(SHOTS.glob("gates-*.png"))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
