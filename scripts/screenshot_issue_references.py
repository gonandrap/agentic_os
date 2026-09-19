"""Screenshot the ISSUE INDEX — both ends of it — for a PR's UI evidence.

`scripts/screenshot_follow_ups.py` is the shape this copies, and the seeding is
deliberately the same so the two pictures are comparable: two rounds, the issues filed
by `ops.file_validation_follow_ups` ITSELF rather than seeded by hand, and only the
panel's verdict faked.

WHAT THIS ADDS, and why one shot would not do. A SECOND work order cites one of those
issues in its brief, so the ranking has something to rank: the project page shows an
issue referenced twice above one referenced once, which is the whole priority signal —
a single-issue shot cannot show an order.

`gh` IS THE SHIPPED FAKE for `screenshot_follow_ups`' reason: the project claims a real
public tracker as its `origin`, so a real `gh` here would open real issues.

Writes PNGs to docs/screenshots/. Everything it touches lives in a temp `JARVIS_HOME`
and a temp catalog, so it never reads or writes the live OS:

    uv run python scripts/screenshot_issue_references.py
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

PR = "https://github.com/gonandrap/agentic_os/pull/263"
JUDGED = "9b2c1d4e5f60718293a4b5c6d7e8f90123456789"
ORIGIN = "https://github.com/gonandrap/agentic_os.git"
ISSUES = "https://github.com/gonandrap/agentic_os/issues"

FINDINGS = [
    {"seat": "maintainer", "round": 1,
     "title": "Name the retry budget in `_repair`'s docstring",
     "detail": "The 3-attempt ceiling is a literal in the loop; a reader has to count "
               "it. Worth a sentence, not worth a round."},
    {"seat": "architect", "round": 1,
     "title": "Fold the two conflict parsers into one",
     "detail": "`_parse_state` and `_merge_state` read the same field with different "
               "fallbacks. They agree today."},
]

ROUND_TWO = [
    {"seat": "tester", "round": 2,
     "title": "The repair axes have no test for the both-failed case",
     "detail": "A conflict and a red check at once is the shape nothing covers."},
]


def _repo(path: Path) -> None:
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "remote", "add", "origin", ORIGIN], cwd=path, check=True)
    (path / "README.md").write_text("# jarvis_os\n")


def _fake_gh(home: Path) -> None:
    import stat

    from jarvis.testing import FAKE_GH

    gdir = home / "fake-gh"
    gdir.mkdir()
    binpath = gdir / "gh"
    binpath.write_text(FAKE_GH)
    binpath.chmod(binpath.stat().st_mode | stat.S_IEXEC)
    os.environ["JARVIS_GH_BIN"] = str(binpath)
    os.environ["PATH"] = f"{gdir}{os.pathsep}{os.environ.get('PATH', '')}"
    os.environ["FAKE_GH_DIR"] = str(gdir)
    os.environ["FAKE_GH_ISSUE_SERIES"] = ISSUES
    os.environ["FAKE_GH_ISSUE_URL"] = f"{ISSUES}/1"
    os.environ["FAKE_GH_LABELS"] = "[]"


def _opinion(store, round_id, seat, verdict, reason, findings):
    store.record_validation_opinion(
        round_id, seat, verdict=verdict, model="claude-opus-5", latency_ms=17200,
        reply=json.dumps({"verdict": verdict, "reason": reason, "asks": [],
                          "findings": findings}, indent=1))


def raiser(store, catalog) -> str:
    """The order whose review filed three issues over two rounds."""
    from jarvis import ops

    wo = ops.create_work_order(
        "jarvis_os", "Heal each pull-request repair axis on its own signal",
        "A conflict and a red check are different failures and share one counter.")
    ops.finish(wo["id"], "opened a pull request", pr_url=PR,
               evidence="uv run pytest tests/ evals/ — 1041 passed")
    cfg = catalog.project("jarvis_os").validation

    first = store.latest_validation_round(wo_id=wo["id"])
    store.set_validation_head(first["id"], JUDGED)
    _opinion(store, first["id"], "maintainer", "reject", "the retry counter is shared",
             [{"severity": "blocker", "title": "One counter serves two failures",
               "detail": "A conflict exhausts the budget a red check then needs."},
              {"severity": "follow_up", "title": FINDINGS[0]["title"],
               "detail": FINDINGS[0]["detail"]}])
    _opinion(store, first["id"], "architect", "pass", "",
             [{"severity": "follow_up", "title": FINDINGS[1]["title"],
               "detail": FINDINGS[1]["detail"]}])
    ops.file_validation_follow_ups(store, catalog.project("jarvis_os"), dict(first),
                                   FINDINGS, cfg, wo_id=wo["id"])
    store.close_validation_round(
        first["id"], "rejected",
        "The repair loop counts one budget for two independent failures, so a merge "
        "conflict that takes three attempts leaves a red check with none.")
    store.add_event(wo["id"], "validation_rejected",
                    {"round": 1, "round_id": first["id"], "of": 3})

    second = store.open_validation_round(wo_id=wo["id"], fingerprint="a41f0c9d72b6",
                                         round=2, summary="split the counters",
                                         evidence="uv run pytest tests/ — 1041 passed")
    store.set_validation_head(second["id"], JUDGED)
    _opinion(store, second["id"], "tester", "pass", "",
             [{"severity": "follow_up", "title": ROUND_TWO[0]["title"],
               "detail": ROUND_TWO[0]["detail"]}])
    ops.file_validation_follow_ups(store, catalog.project("jarvis_os"), dict(second),
                                   ROUND_TWO, cfg, wo_id=wo["id"])
    store.close_validation_round(second["id"], "passed", "")
    store.add_event(wo["id"], "validation_passed", {"round": 2, "round_id": second["id"]})
    store.set_status(wo["id"], "waiting_pr_merge")
    return str(wo["id"])


def citer() -> None:
    """A later order whose brief cites one of them. This is what makes a count a rank."""
    from jarvis import ops

    ops.create_work_order(
        "jarvis_os", "Cover the both-failed repair case",
        f"Same ground as {ISSUES}/3 — a conflict and a red check at once is the shape "
        f"nothing covers, and the repair loop takes a different path for each.")


def seed() -> tuple[Path, str]:
    from jarvis import ops
    from jarvis.catalog import load_catalog
    from jarvis.central_store import CentralStore
    from jarvis.project_store import ProjectStore

    home = Path(tempfile.mkdtemp())
    _fake_gh(home)
    project = home / "jarvis_os"
    project.mkdir(parents=True)
    _repo(project)
    catalog = home / "catalog.json"
    catalog.write_text(json.dumps({
        "os": {"ui": {"port": 8787}},
        "projects": [{"name": "jarvis_os", "path": str(project),
                      "description": "the OS itself",
                      "validation": {"enabled": True}}],
    }, indent=2))
    store = CentralStore()
    store.set_state("catalog_path", str(catalog))
    store.close()
    ops.start_os(str(catalog), foreground=True)

    pstore = ProjectStore(project)
    wo_id = raiser(pstore, load_catalog(catalog))
    pstore.close()
    citer()
    # The sweep is what reads each issue's state and writes the count back. Run one
    # tick's worth of it, so the pictures show what a live fleet shows.
    from jarvis.daemon import Daemon

    daemon = Daemon(load_catalog(catalog))
    pstore = ProjectStore(project)
    daemon.sync_issue_references(load_catalog(catalog).projects[0], pstore)
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
        page.locator("h2", has_text="Tracker issues").first.scroll_into_view_if_needed()
        page.wait_for_timeout(200)
        page.screenshot(path=SHOTS / "issue-index-work-order.png")
        page.goto(f"http://127.0.0.1:{PORT}/project/jarvis_os")
        page.locator("h2", has_text="Tracker issues").first.scroll_into_view_if_needed()
        page.wait_for_timeout(200)
        page.screenshot(path=SHOTS / "issue-index-project.png")
        browser.close()


def main() -> int:
    os.environ["JARVIS_HOME"] = tempfile.mkdtemp()
    os.environ.pop("JARVIS_WO_ID", None)
    sys.path.insert(0, str(REPO / "src"))
    _catalog, wo_id = seed()
    threading.Thread(target=serve, daemon=True).start()
    time.sleep(2)
    shoot(wo_id)
    print("\n".join(str(q) for q in sorted(SHOTS.glob("issue-index-*.png"))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
