"""Screenshot the two investigation pages a reviewer has to see, for a PR's evidence.

§2.9 of docs/superpowers/specs/2026-09-27-investigation-orders.md.
`scripts/screenshot_improvement_order.py` is the shape it copies. Two investigations are
seeded, because the two verdicts render differently and settle differently: a GAP that was
FILED (classification, subject, root cause, evidence, issue url + the work order the
expedited filing dispatched) and a WAITING_ON_USER (the one classification that raises
attention, showing what the user owes).

The state is built through `ops.create_investigation_order` / `ops.submit_verdict` and
`jarvis.testing.a_verdict`, never by writing rows: the filing, the duplicate check and the
attention flag are decisions `ops` owns, and a hand-written row would picture a state the
OS cannot reach.

`gh` IS THE SHIPPED FAKE (`scripts/screenshot_issue_references.py`' reason): a GAP files an
EXPEDITED bug, so a real `gh` here would open a real issue on the real tracker. Everything
else lives in a throwaway JARVIS_HOME, so it never touches the live OS:

    uv run python scripts/screenshot_investigation_order.py
"""

from __future__ import annotations

import json
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SHOTS = REPO / "docs" / "screenshots"
PORT = 8812
PROJECT = "jarvis_os"

BUG_REPO = "gonandrap/agentic_os"
ORIGIN = f"https://github.com/{BUG_REPO}.git"
ISSUE = f"https://github.com/{BUG_REPO}/issues/812"

# Both `why`s name their own subject: the page shows the id beside the text, and a
# screenshot whose two ids disagree reads as a bug in the page.
GAP_WHY = ("{subject} has been `validating` for six hours with no turn in flight and no "
           "open Neo question. Find out what is holding it.")
WAITING_WHY = ("{subject} has not moved in four hours and its worker is idle. Nothing on "
               "the timeline says what it is waiting for.")
WAITING_CAUSE = ("Nothing is holding it mechanically: assumption as-4 has been pending "
                 "review since the first round, and the worker will not take another "
                 "turn until it is decided.")


def _fake_gh(home: Path) -> None:
    """The fake `gh` binary from the test harness, installed for a plain process.

    `jarvis.testing.fake_gh` is a pytest fixture (it needs `monkeypatch`), so a script
    installs the same FAKE_GH body itself rather than depending on pytest.
    """
    from jarvis.testing import FAKE_GH

    gdir = home / "fake-gh"
    gdir.mkdir()
    binpath = gdir / "gh"
    binpath.write_text(FAKE_GH)
    binpath.chmod(binpath.stat().st_mode | stat.S_IEXEC)
    os.environ["JARVIS_GH_BIN"] = str(binpath)
    os.environ["PATH"] = f"{gdir}{os.pathsep}{os.environ.get('PATH', '')}"
    os.environ["FAKE_GH_DIR"] = str(gdir)
    os.environ["FAKE_GH_ISSUE_URL"] = ISSUE
    os.environ["JARVIS_BUG_REPO"] = BUG_REPO
    # The fake READS STDIN on every call, so a run whose stdin is an open pipe leaves it
    # blocking and the duplicate check dies on its 30s timeout. Hand every child an empty
    # one instead of depending on how the script was launched.
    os.dup2(os.open(os.devnull, os.O_RDONLY), 0)


def _subject(store, title: str, description: str) -> str:
    """A stuck work order — what an investigation is opened ON."""
    wo = store.create_work_order(title, description=description)
    store.update_work_order(wo["id"], status="validating")
    return str(wo["id"])


def _investigating(store, inv_id: str, subject: str) -> str:
    """The state the daemon leaves an investigation in: one investigator, `planning`."""
    child = store.create_work_order(f"investigate {subject}", kind="investigator",
                                    parent_id=inv_id, status="running",
                                    description="read-only diagnosis")
    store.update_feature_order(inv_id, plan_wo_id=child["id"])
    store.set_feature_status(inv_id, "planning")
    return str(child["id"])


def seed() -> tuple[str, str]:
    """A GAP investigation whose bug was filed, and a WAITING_ON_USER one."""
    from jarvis import ops
    from jarvis.central_store import CentralStore
    from jarvis.project_store import ProjectStore
    from jarvis.testing import a_verdict, make_git_project

    home = Path(os.environ["JARVIS_HOME"])
    _fake_gh(home)
    project = make_git_project(home, PROJECT)
    # `issues.tracker_project` resolves the project by its git `origin`, and without it a
    # GAP's expedited filing has no project to dispatch the work order into.
    subprocess.run(["git", "remote", "add", "origin", ORIGIN], cwd=project, check=True)
    catalog = home / "catalog.json"
    catalog.write_text(json.dumps({
        "os": {"defaults": {"model": "opus"}, "ui": {"port": 8787},
               "notifications": {"sinks": ["log"]}},
        "projects": [{"name": PROJECT, "path": str(project),
                      "description": "the OS itself"}],
    }, indent=2))
    central = CentralStore()
    central.set_state("catalog_path", str(catalog))
    central.close()
    ops.start_os(str(catalog), foreground=True)

    store = ProjectStore(project)
    try:
        gap_subject = _subject(
            store, "Heal each pull-request repair axis on its own signal",
            "A merge conflict and a red check are different failures sharing one counter.")
        waiting_subject = _subject(
            store, "Ship the CSV export",
            "One endpoint, streamed, with the column set the dashboard already renders.")
    finally:
        store.close()

    gap = ops.create_investigation_order(PROJECT, gap_subject,
                                        GAP_WHY.format(subject=gap_subject))
    waiting = ops.create_investigation_order(PROJECT, waiting_subject,
                                             WAITING_WHY.format(subject=waiting_subject))
    store = ProjectStore(project)
    try:
        _investigating(store, gap["id"], gap_subject)
        _investigating(store, waiting["id"], waiting_subject)
    finally:
        store.close()

    # WAITING_ON_USER first: it files nothing, so it cannot become the GAP's duplicate.
    ops.submit_verdict(waiting["id"], a_verdict("WAITING_ON_USER",
                                                subject=waiting_subject,
                                                root_cause=WAITING_CAUSE,
                                                evidence=[{
                                                    "source": f"jarvis wo show "
                                                              f"{waiting_subject}",
                                                    "quote": "as-4 pending review — the "
                                                             "worker has taken no turn "
                                                             "since"}]))
    out = ops.submit_verdict(gap["id"], a_verdict("GAP", subject=gap_subject))
    if not (out.get("filed") or {}).get("issue_url"):
        raise SystemExit(f"the GAP was not filed, so there is nothing to shoot: {out}")
    return gap["id"], waiting["id"]


def serve() -> None:
    import uvicorn

    from jarvis.ui.app import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="warning")


def shoot(gap: str, waiting: str) -> None:
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    base = f"http://127.0.0.1:{PORT}"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 820})
        for inv_id, name in ((gap, "investigation-order-gap-filed.png"),
                             (waiting, "investigation-order-waiting-on-user.png")):
            page.goto(f"{base}/inv/{PROJECT}/{inv_id}")
            page.screenshot(path=SHOTS / name, full_page=True)
        browser.close()


def main() -> int:
    home = tempfile.mkdtemp()
    os.environ["JARVIS_HOME"] = home
    os.environ["JARVIS_TRANSCRIPT_ROOT"] = str(Path(home) / "transcripts")
    # Inside a worker's process tree `JARVIS_SPEND_HOME` names the LIVE os.db — see
    # `scripts/screenshot_improvement_order.py`.
    os.environ["JARVIS_SPEND_HOME"] = home
    os.environ.pop("JARVIS_WO_ID", None)
    sys.path.insert(0, str(REPO / "src"))
    gap, waiting = seed()
    threading.Thread(target=serve, daemon=True).start()
    time.sleep(2)
    shoot(gap, waiting)
    print("\n".join(str(SHOTS / name) for name in (
        "investigation-order-gap-filed.png",
        "investigation-order-waiting-on-user.png")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
