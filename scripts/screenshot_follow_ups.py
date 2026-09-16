"""Screenshot a round's FILED FOLLOW-UPS on the work-order page, for a PR's UI evidence.

`scripts/screenshot_forced_round.py` is the shape this copies. Two rounds again, and the
pair is the whole claim: round 1 rejected over a blocker and filed two follow-ups, round 2
passed and filed none. A shot of the filing round alone would not show that an ordinary
round renders nothing here — which is every round on a fleet that has the panel off.

**THE BACKLOG ROWS AND THE EVENT ARE WRITTEN BY `ops.file_validation_follow_ups` ITSELF**,
not seeded by hand. The picture is then evidence of the path the daemon takes: the
origin columns, the note naming the seat, the cap and the dedupe are whatever that
function really produces. Only the panel's VERDICT is faked, because a real one would
need five model calls.

Writes PNGs to docs/screenshots/. Everything it touches lives in a temp `JARVIS_HOME`
and a temp catalog, so it never reads or writes the live OS:

    uv run python scripts/screenshot_follow_ups.py
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

PR = "https://github.com/gonandrap/agentic_os/pull/263"
JUDGED = "9b2c1d4e5f60718293a4b5c6d7e8f90123456789"

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


def _repo(path: Path) -> None:
    """An empty checkout, the shape `testing.make_git_project` builds for the suite."""
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    (path / "README.md").write_text("# jarvis_os\n")


def order(store, catalog) -> str:
    """One work order with a round that filed, and a round that did not."""
    from jarvis import ops

    wo = ops.create_work_order(
        "jarvis_os", "Heal each pull-request repair axis on its own signal",
        "A conflict and a red check are different failures and share one counter.")
    ops.finish(wo["id"], "opened a pull request", pr_url=PR,
               evidence="uv run pytest tests/ evals/ — 1041 passed")
    cfg = catalog.project("jarvis_os").validation

    first = store.latest_validation_round(wo_id=wo["id"])
    assert first is not None
    store.set_validation_head(first["id"], JUDGED)
    store.record_validation_opinion(
        first["id"], "maintainer", verdict="reject", model="claude-opus-5",
        latency_ms=18400,
        reply=json.dumps({"verdict": "reject", "reason": "the retry counter is shared",
                          "asks": ["split the counter"],
                          "findings": [
                              {"severity": "blocker",
                               "title": "One counter serves two failures",
                               "detail": "A conflict exhausts the budget a red check "
                                         "then needs."},
                              {"severity": "follow_up", "title": FINDINGS[0]["title"],
                               "detail": FINDINGS[0]["detail"]}]}, indent=1))
    store.record_validation_opinion(
        first["id"], "architect", verdict="pass", model="claude-opus-5", latency_ms=16100,
        reply=json.dumps({"verdict": "pass", "reason": "", "asks": [],
                          "findings": [{"severity": "follow_up",
                                        "title": FINDINGS[1]["title"],
                                        "detail": FINDINGS[1]["detail"]}]}, indent=1))
    ops.file_validation_follow_ups(store, "jarvis_os", dict(first), FINDINGS, cfg,
                                   wo_id=wo["id"])
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
    store.record_validation_opinion(second["id"], "maintainer", verdict="pass",
                                    reply="Each axis carries its own attempt count now.",
                                    model="claude-opus-5", latency_ms=15900)
    store.record_validation_opinion(second["id"], "architect", verdict="pass",
                                    reply="No new authority reaches a worker.",
                                    model="claude-opus-5", latency_ms=14200)
    store.close_validation_round(second["id"], "passed", "")
    store.add_event(wo["id"], "validation_passed", {"round": 2, "round_id": second["id"]})
    store.set_status(wo["id"], "waiting_pr_merge")
    return str(wo["id"])


def seed() -> tuple[Path, str]:
    from jarvis import ops
    from jarvis.catalog import load_catalog
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
                      "validation": {"enabled": True}}],
    }, indent=2))
    store = CentralStore()
    store.set_state("catalog_path", str(catalog))
    store.close()
    ops.start_os(str(catalog), foreground=True)

    pstore = ProjectStore(project)
    wo_id = order(pstore, load_catalog(catalog))
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
        page.screenshot(path=SHOTS / "validation-follow-ups.png")
        # The TIMELINE is the other rendered surface: an unlabelled event kind renders
        # as the bare kind beside a JSON blob, which is what the new branch prevents.
        page.get_by_role("tab", name="Timeline").click()
        page.wait_for_timeout(200)
        page.locator("#tab-timeline").scroll_into_view_if_needed()
        page.wait_for_timeout(200)
        page.screenshot(path=SHOTS / "validation-follow-ups-timeline.png")
        browser.close()


def main() -> int:
    os.environ["JARVIS_HOME"] = tempfile.mkdtemp()
    os.environ.pop("JARVIS_WO_ID", None)
    sys.path.insert(0, str(REPO / "src"))
    _catalog, wo_id = seed()
    threading.Thread(target=serve, daemon=True).start()
    time.sleep(2)
    shoot(wo_id)
    print("\n".join(str(q) for q in sorted(SHOTS.glob("validation-follow-ups*.png"))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
