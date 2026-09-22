"""Screenshot a round's FILED FOLLOW-UPS on the work-order page, for a PR's UI evidence.

`scripts/screenshot_forced_round.py` is the shape this copies. Two rounds again, and the
pair is the whole claim: round 1 rejected over a blocker and filed two follow-ups, round 2
passed and filed none. A shot of the filing round alone would not show that an ordinary
round renders nothing here — which is every round on a fleet that has the panel off.

**THE ISSUES AND THE EVENT ARE FILED BY `ops.file_validation_follow_ups` ITSELF**, not
seeded by hand. The picture is then evidence of the path the daemon takes: the issue
links, the cap, the dedupe and the failure counting are whatever that function really
produces. Only the panel's VERDICT is faked, because a real one would need five model
calls.

**`gh` IS THE SHIPPED FAKE (`testing.FAKE_GH`), TWO WAYS.** Follow-ups are filed on the
project under review's own tracker, and this script's project claims `gonandrap/agentic_os`
as its `origin` so the shot shows plausible issue numbers — which is a PUBLIC tracker, so
a real `gh` here would open real issues. `JARVIS_GH_BIN` points at the fake and the fake's
directory goes on `PATH` as well: the belt is the env var `issues.py` reads, the braces
are for anything that ever shells out to a bare `gh`.

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

#: EVERY ONE ADMISSIBLE — `file` and `failure` are what `ops.follow_up_admissible`
#: requires before a finding may become a ticket (spec §10.5), and a script seeded with
#: pre-§10 findings would photograph an empty section.
FINDINGS = [
    {"seat": "maintainer", "round": 1,
     "title": "`_repair` retries a conflict against the check budget",
     "detail": "The 3-attempt ceiling is one literal shared by both axes.",
     "file": "src/jarvis/landing.py", "symbol": "_repair",
     "failure": "a conflict that takes three attempts leaves a red check with none"},
    {"seat": "architect", "round": 1,
     "title": "`_parse_state` and `_merge_state` disagree on a missing field",
     "detail": "They read the same field with different fallbacks. They agree today.",
     "file": "src/jarvis/landing.py", "symbol": "_parse_state",
     "failure": "a payload with no `state` key parses as CONFLICTING in one and as "
                "UNKNOWN in the other"},
]


#: The finding the second order raises, on a tracker that may be public.
WITHHELD = [
    {"seat": "security", "round": 1,
     "title": "`_refund` credits a budget the gate never debited",
     "detail": "The refund runs on every denial, including one that never spent an "
               "attempt.",
     "file": "src/jarvis/landing.py", "symbol": "_refund",
     "failure": "a denied gate on attempt 1 leaves the order with four attempts of a "
                "three-attempt budget"},
]

ORIGIN = "https://github.com/gonandrap/agentic_os.git"
ISSUES = "https://github.com/gonandrap/agentic_os/issues"


def _repo(path: Path) -> None:
    """An empty checkout, the shape `testing.make_git_project` builds for the suite.

    WITH AN `origin`, unlike that helper: `ops.follow_up_repo` reads the remote to decide
    where the issues go, and a checkout without one exercises the failure path instead —
    which is a different picture (and one this script's sibling assertion in
    `tests/test_validation_follow_ups.py` already covers).
    """
    import subprocess

    subprocess.run(["git", "init", "-q"], cwd=path, check=True)
    subprocess.run(["git", "remote", "add", "origin", ORIGIN], cwd=path, check=True)
    (path / "README.md").write_text("# jarvis_os\n")


def _fake_gh(home: Path) -> None:
    """Install the suite's own fake `gh`, so nothing here can reach a public tracker."""
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
    os.environ["FAKE_GH_ISSUE_SERIES"] = ISSUES   # one issue number per create
    os.environ["FAKE_GH_ISSUE_URL"] = f"{ISSUES}/1"
    os.environ["FAKE_GH_LABELS"] = "[]"           # the label does not exist there yet


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


def withheld_order(store, catalog) -> str:
    """One work order whose follow-up may NOT be published, so none was filed.

    `FAKE_GH_PRIVATE=0` is the whole difference: the privacy read answers PUBLIC, the
    seat's words stay off the tracker, and — since spec §10 — the issue stays off it too.
    The finding is kept whole on the internal record and the page says so.
    """
    from jarvis import ops

    wo = ops.create_work_order(
        "jarvis_os", "Count a repair attempt only where the worker was allowed one",
        "A gate that blocked the worker still spent its repair budget.")
    ops.finish(wo["id"], "opened a pull request", pr_url=PR,
               evidence="uv run pytest tests/ evals/ — 1041 passed")
    cfg = catalog.project("jarvis_os").validation
    rnd = store.latest_validation_round(wo_id=wo["id"])
    assert rnd is not None
    store.set_validation_head(rnd["id"], JUDGED)
    store.record_validation_opinion(
        rnd["id"], "tester", verdict="pass", model="claude-opus-5", latency_ms=15200,
        reply="Nothing here blocks; one remark filed rather than argued.")
    os.environ["FAKE_GH_PRIVATE"] = "0"
    try:
        ops.file_validation_follow_ups(store, catalog.project("jarvis_os"), dict(rnd),
                                       WITHHELD, cfg, wo_id=wo["id"])
    finally:
        os.environ.pop("FAKE_GH_PRIVATE", None)
    store.close_validation_round(rnd["id"], "passed", "")
    store.add_event(wo["id"], "validation_passed", {"round": 1, "round_id": rnd["id"]})
    store.set_status(wo["id"], "waiting_pr_merge")
    return str(wo["id"])


def seed() -> tuple[Path, str, str]:
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
    loaded = load_catalog(catalog)
    wo_id = order(pstore, loaded)
    kept_id = withheld_order(pstore, loaded)
    pstore.close()
    return catalog, wo_id, kept_id


def serve() -> None:
    import uvicorn

    from jarvis.ui.app import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="warning")


def shoot(wo_id: str, kept_id: str) -> None:
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})
        page.goto(f"http://127.0.0.1:{PORT}/wo/jarvis_os/{wo_id}")
        page.locator("h2", has_text="Tracker issues").first.scroll_into_view_if_needed()
        page.wait_for_timeout(200)
        page.screenshot(path=SHOTS / "validation-follow-ups.png")
        # THE OTHER HALF OF THE SAME DECISION (spec §10.1): on a repository the OS
        # cannot establish is private, no issue is opened at all and the finding is on
        # the internal record. A shot of the filing case alone would not show it.
        page.goto(f"http://127.0.0.1:{PORT}/wo/jarvis_os/{kept_id}")
        page.locator("h2", has_text="Tracker issues").first.scroll_into_view_if_needed()
        page.wait_for_timeout(200)
        page.screenshot(path=SHOTS / "validation-follow-ups-kept.png")
        page.goto(f"http://127.0.0.1:{PORT}/wo/jarvis_os/{wo_id}")
        page.wait_for_timeout(200)
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
    _catalog, wo_id, kept_id = seed()
    threading.Thread(target=serve, daemon=True).start()
    time.sleep(2)
    shoot(wo_id, kept_id)
    print("\n".join(str(q) for q in sorted(SHOTS.glob("validation-follow-ups*.png"))))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
