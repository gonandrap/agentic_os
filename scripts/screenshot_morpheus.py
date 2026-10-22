"""Screenshot `/morpheus` with a release's PRs in every state, for a PR's UI evidence (docs/superpowers/specs/2026-10-21-morpheus-validates-releases-in-production.md)"""

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
PORT = 8805


def seed() -> None:
    from jarvis import morpheus
    from jarvis.catalog import MorpheusConfig
    from jarvis.central_store import CentralStore

    cfg = MorpheusConfig()
    central = CentralStore()
    now = time.time()
    rows = [
        (1130, "[wo-1a2b3c4d] Neo answers alarms in one call",
         "Watch Neo answer an alarm.\n\n```bash\njarvis neo stats --json | jq -e '.alarm.calls_per_answer <= 1' || exit 75\n```",
         0, "validated"),
        (1131, "[wo-5e6f7a8b] The stuck sweep skips budget-capped orders",
         "```bash\njarvis stuck --json | jq -e 'all(.[]; .excluded != null)' && exit 0 || exit 1\n```",
         1, "failed"),
        (1132, "[wo-9c0d1e2f] Release notes list every PR",
         "A release page lists one line per PR — check the next `gh release view`.", None,
         "queued"),
        (1133, "Trim comments main gained since the last merge",
         "None — comments only, production has nothing to show", None, "validated"),
        (1134, "[wo-3a4b5c6d] Gate classifier stops flagging `git status`",
         "```bash\ngrep -c 'git status' \"$JARVIS_HOME/logs/gates.log\" | grep -q '^0$' || exit 75\n```",
         75, "monitoring"),
    ]
    for n, title, section, code, _state in rows:
        body = f"## Summary\nx\n\n## {morpheus.SECTION}\n\n{section}\n"
        item = morpheus.enqueue(central, cfg, morpheus.PullRequestBody(
            n, f"https://github.com/gonandrap/agentic_os/pull/{n}", title, body),
            repo="gonandrap/agentic_os", release_tag="jarvis-0.9.4", now=now - 3600)
        if code is not None and item["state"] == "monitoring":
            morpheus.check(central, cfg, item, Path.cwd(), now=now,
                           run=lambda s, c, t, code=code: morpheus.CheckResult(
                               code, {0: "1 call per answer", 1: "wo-77aa…: excluded=null",
                                      75: "3"}[code]))
    failed = [i for i in central.morpheus_items(("failed",))][0]
    central.claim_morpheus_action(failed["id"], "investigation:fo-81c2d3e4")
    central.set_state(morpheus.RELEASE_KEY, "jarvis-0.9.4")
    central.set_state(morpheus.SESSION_KEY, json.dumps({
        "session_id": "4f1c9a2e-7b3d-4e8f-9a1b-2c3d4e5f6a7b", "turns": 7, "compactions": 1,
        "context_share": 0.18, "context_peak": 36000, "context_window": 200000,
        "last_turn_ts": now - 900, "last_turn_kind": "scripts", "compact_due": False}))
    central.close()


def serve() -> None:
    import uvicorn

    from jarvis.ui.app import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="warning")


def shoot() -> None:
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1440, "height": 1000})
        page.goto(f"http://127.0.0.1:{PORT}/morpheus")
        page.screenshot(path=SHOTS / "morpheus.png", full_page=True)
        browser.close()


def main() -> int:
    os.environ["JARVIS_HOME"] = tempfile.mkdtemp()
    os.environ.pop("JARVIS_WO_ID", None)
    sys.path.insert(0, str(REPO / "src"))
    seed()
    threading.Thread(target=serve, daemon=True).start()
    time.sleep(2)
    shoot()
    print(SHOTS / "morpheus.png")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
