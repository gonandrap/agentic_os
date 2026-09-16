"""Screenshot a FORCED validation round on the work-order page, for a PR's UI evidence.

`scripts/screenshot_automerge.py` is the shape this copies. Two rounds on one work order,
because the pair is the whole claim: round 1 judged before `head_sha` shipped, carrying no
commit and therefore unable to auto-merge, and round 2 opened by hand — marked `forced`,
carrying the reason, and carrying the commit that unblocks the merge. A shot of the forced
round alone would not show what it is different FROM.

Writes PNGs to docs/screenshots/. Everything it touches lives in a temp `JARVIS_HOME` and
a temp catalog, so it never reads or writes the live OS:

    uv run python scripts/screenshot_forced_round.py
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
PORT = 8802

PR = "https://github.com/gonandrap/agentic_os/pull/242"
JUDGED = "4afb5e7b1c2d3e4f5061728394a5b6c7d8e9f012"
REASON = "round 1 was judged before jarvis-0.10.0 and recorded no commit"


def _repo(path: Path) -> None:
    """An empty checkout, the shape `testing.make_git_project` builds for the suite."""
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    (path / "README.md").write_text("# jarvis_os\n")


def order(store) -> str:
    """One work order parked behind a pull request, with the pair of rounds.

    **ROUND 2 IS OPENED BY `ops.force_validation` ITSELF**, not written here. The shot is
    evidence of the path the operator actually takes — the `forced_reason` on the round
    and the `validation_forced` event are whatever that function really produces, so a
    change that stopped writing either shows up in the picture instead of being papered
    over by hand-inserted rows. Only the panel's VERDICT is faked, because a real one
    would need five model calls.

    Round 1 is settled the way the pre-0.10.0 daemon settled every round: a verdict, and
    NO commit beside it. That is the state the whole feature exists to get out of, so
    seeding it with a commit would make the shot a picture of something else.
    """
    from jarvis import ops

    wo = ops.create_work_order(
        "jarvis_os", "A negative slice is not a privileged action",
        "Teach the gate recogniser that `log[-20:]` ships nothing.")
    ops.finish(wo["id"], "opened a pull request", pr_url=PR,
               evidence="uv run pytest tests/ evals/ — 1041 passed")
    first = store.latest_validation_round(wo_id=wo["id"]) \
        or store.open_validation_round(wo_id=wo["id"], fingerprint="7c1f9a2e04b3")
    store.close_validation_round(first["id"], "passed", "")
    store.record_validation_opinion(first["id"], "tester", verdict="pass",
                                    reply="The recogniser's boundary is pinned.",
                                    model="claude-opus-5", latency_ms=19200)
    # `force_validation` only accepts FORCEABLE_STATUSES, so the work order has to be
    # parked exactly as the real one is before the command will touch it.
    store.set_status(wo["id"], "waiting_pr_merge")

    ops.force_validation(wo["id"], reason=REASON, project_name="jarvis_os")

    forced = store.latest_validation_round(wo_id=wo["id"])
    assert forced is not None and forced["forced_reason"] == REASON
    # The panel's half, faked: `Daemon._validate_work_order` would record the head from
    # the evidence packet and close the round, and neither needs a model to be a picture.
    store.set_validation_head(forced["id"], JUDGED)
    store.close_validation_round(forced["id"], "passed", "")
    store.record_validation_opinion(forced["id"], "tester", verdict="pass",
                                    reply="Same diff, now bound to a commit.",
                                    model="claude-opus-5", latency_ms=17600)
    store.record_validation_opinion(forced["id"], "security", verdict="pass",
                                    reply="No new authority reaches a worker.",
                                    model="claude-opus-5", latency_ms=15100)
    store.add_event(wo["id"], "automerge_proposed",
                    {"approval_id": 44, "neo_question_id": 91, "round_id": forced["id"],
                     "round": 2, "head_sha": JUDGED, "pr_url": PR})
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
        page.screenshot(path=SHOTS / "forced-round.png")
        # The TIMELINE is the other rendered surface this touches, and the one the whole
        # command is about: it is where a forced re-judgement would otherwise read like a
        # worker re-delivering. Behind a tab, so it has to be clicked.
        page.get_by_role("tab", name="Timeline").click()
        page.wait_for_timeout(200)
        page.locator("#tab-timeline").scroll_into_view_if_needed()
        page.wait_for_timeout(200)
        page.screenshot(path=SHOTS / "forced-round-timeline.png")
        browser.close()


def main() -> int:
    os.environ["JARVIS_HOME"] = tempfile.mkdtemp()
    os.environ.pop("JARVIS_WO_ID", None)
    sys.path.insert(0, str(REPO / "src"))
    _catalog, wo_id = seed()
    threading.Thread(target=serve, daemon=True).start()
    time.sleep(2)
    shoot(wo_id)
    print("\n".join(str(q) for q in sorted(SHOTS.glob("forced-round*.png"))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
