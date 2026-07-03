"""Seed a realistic fixture fleet, run the REAL Jarvis UI, photograph it.

The screenshots in the promo are the actual product rendering actual DB state —
the fleet is fixture (fake `claude` supervisor for speed/determinism) but every
pixel comes from the real dashboard. Run via promo/render.py, or standalone:

    uv run python promo/capture_screens.py   # → promo/out/screens/*.png
"""

from __future__ import annotations

import json
import os
import shutil
import socket
import stat
import subprocess
import threading
import time
from pathlib import Path

OUT = Path(__file__).parent / "out"
SCREENS = OUT / "screens"
FIXTURE = OUT / "fixture"

VIEWPORT = {"width": 1520, "height": 860}  # framed inside the 1920×1080 canvas


def build_world() -> dict:
    """Fixture home, trusted projects, fake claude on JARVIS_CLAUDE_BIN."""
    from jarvis.testing import FAKE_CLAUDE

    if FIXTURE.exists():
        shutil.rmtree(FIXTURE)
    home = FIXTURE / "jarvis-home"
    fdir = FIXTURE / "fake-claude"
    (fdir / "jobs").mkdir(parents=True)
    binpath = fdir / "claude"
    binpath.write_text(FAKE_CLAUDE)
    binpath.chmod(binpath.stat().st_mode | stat.S_IEXEC)
    claude_json = FIXTURE / "claude.json"

    os.environ.update({
        "JARVIS_HOME": str(home),
        "JARVIS_CLAUDE_BIN": str(binpath),
        "FAKE_CLAUDE_DIR": str(fdir),
        "JARVIS_CLAUDE_JOBS_DIR": str(fdir / "jobs"),
        "JARVIS_CLAUDE_JSON": str(claude_json),
    })

    projects = {}
    trust = {"projects": {}}
    for name, desc in [
        ("webapp", "Customer-facing web application"),
        ("etl-pipeline", "Nightly data pipeline"),
        ("docs-site", "Product documentation"),
    ]:
        p = FIXTURE / "workspace" / name
        p.mkdir(parents=True)
        subprocess.run(["git", "init", "-q"], cwd=p, check=True)
        (p / "README.md").write_text(f"# {name}\n\n{desc}\n")
        trust["projects"][str(p)] = {"hasTrustDialogAccepted": True}
        projects[name] = p
    claude_json.write_text(json.dumps(trust))

    catalog = FIXTURE / "catalog.json"
    catalog.write_text(json.dumps({
        "os": {"defaults": {"model": "sonnet"}, "neo": {"model": "opus"}},
        "projects": [
            {"name": n, "path": str(p), "description": d}
            for (n, d), p in zip(
                [("webapp", "Customer-facing web application"),
                 ("etl-pipeline", "Nightly data pipeline"),
                 ("docs-site", "Product documentation")], projects.values())
        ],
    }))
    return {"catalog": catalog, "projects": projects}


def seed(world: dict) -> dict:
    """Drive the real pipeline into a photogenic mid-flight state."""
    from jarvis import ops
    from jarvis.catalog import load_catalog
    from jarvis.central_store import CentralStore
    from jarvis.daemon import Daemon
    from jarvis.neo_store import NeoStore
    from jarvis.project_store import ProjectStore

    ops.start_os(str(world["catalog"]), foreground=True)
    daemon = Daemon(load_catalog(world["catalog"]))
    # the header should show the healthy '● daemon up' — this process IS the daemon
    from jarvis.paths import daemon_pidfile
    daemon_pidfile().write_text(str(os.getpid()))

    # webapp: one running, one finished-with-assumption (needs review)
    wo_run = ops.create_work_order(
        "webapp", "Fix password reset link expiring after 5 minutes",
        description="Users report the emailed reset link is already invalid when "
                    "they click it. Repro on staging. Ship with a regression test.")
    wo_rev = ops.create_work_order(
        "webapp", "Add CSV export to the reports page",
        description="Finance wants the filtered report table as CSV.")
    # etl: completed earlier
    wo_done = ops.create_work_order(
        "etl-pipeline", "Backfill June order data",
        description="Warehouse missed 3 days of orders; backfill idempotently.")
    # docs: waiting in queue
    ops.create_work_order("docs-site", "Regenerate the API reference for v0.4")
    daemon.tick()

    ops.assume(wo_rev["id"], "Export respects the user's saved column filters "
                             "(not the full table) — matches how PDF export works")
    ops.finish(wo_rev["id"], "CSV export shipped behind the reports menu; "
                             "PR #214 opened with tests")
    ops.finish(wo_done["id"], "Backfill complete: 41,209 orders restored, "
                              "checkpoints in s3://etl-state/backfills/")

    # Neo: a reviewed answer, a fresh answer awaiting review, and an escalation
    neo = NeoStore()
    try:
        q1 = neo.ask("webapp", wo_rev["id"],
                     "Should the CSV export default to comma or semicolon?")
        neo.record_answer(q1["id"],
                          "Comma. Add ?delimiter=; for the EU locale export "
                          "instead of changing the default.",
                          reason="matches the exports-default-to-CSV preference")
        neo.review(q1["id"], approved=True)
        q2 = neo.ask("etl-pipeline", wo_done["id"],
                     "Keep per-day backfill checkpoints, or one checkpoint per run?")
        neo.record_answer(q2["id"],
                          "Per-day. Reruns stay idempotent and a failed day "
                          "doesn't invalidate the whole backfill.",
                          reason="restartability beats tidiness here")
        q3 = neo.ask("etl-pipeline", wo_done["id"],
                     "The backfill needs ~$40 of extra warehouse credits — proceed?")
        neo.mark(q3["id"], "escalated", reason="spending money is the user's call")
        neo.add_learning("Prefer the stdlib over adding dependencies.",
                         source="review", project="")
        neo.add_learning("Exports default to CSV; JSON only when nesting is "
                         "unavoidable.", source="review", project="webapp")
    finally:
        neo.close()

    central = CentralStore()
    try:
        central.add_inbox("etl-pipeline",
                          f"Neo escalated a question from {wo_done['id']}",
                          body="The backfill needs ~$40 of extra warehouse credits "
                               "— proceed?", level="warning", wo_id=wo_done["id"])
        central.add_inbox("webapp", "PR #214 opened: CSV export",
                          body="Worker finished and opened the PR for review.",
                          level="info", wo_id=wo_rev["id"])
        bl1 = central.add_backlog("etl-pipeline", "Pin Python to 3.12 in CI")
        central.add_backlog("etl-pipeline", "Migrate CI to uv",
                            depends_on=[bl1["id"]])
        central.add_knowledge("Deploys go out Tuesdays; never ship Friday.",
                              project="", topic="process")
    finally:
        central.close()

    # make the running worker visibly RUNNING and give its timeline some life
    store = ProjectStore(world["projects"]["webapp"])
    try:
        store.add_event(wo_run["id"], "status", {"status": "running"})
        store.add_event(wo_run["id"], "progress",
                        {"note": "reproduced: token TTL read from the wrong config key"})
        # age things a little so the UI doesn't scream "0s"
        store.conn.execute("UPDATE work_orders SET created_at = created_at - 1560")
        store.conn.execute("UPDATE wo_events SET ts = ts - 900")
    finally:
        store.close()
    return {"daemon": daemon, "wo_run": wo_run, "wo_rev": wo_rev,
            "wo_done": wo_done, "q3_id": q3["id"]}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def serve() -> tuple[str, object]:
    import uvicorn

    from jarvis.ui.app import create_app

    port = _free_port()
    config = uvicorn.Config(create_app(), host="127.0.0.1", port=port,
                            log_level="critical")
    srv = uvicorn.Server(config)
    threading.Thread(target=srv.run, daemon=True).start()
    while not srv.started:
        time.sleep(0.05)
    return f"http://127.0.0.1:{port}", srv


def capture() -> None:
    from playwright.sync_api import sync_playwright

    from jarvis import ops

    SCREENS.mkdir(parents=True, exist_ok=True)
    world = build_world()
    state = seed(world)
    base, srv = serve()

    with sync_playwright() as pw:
        browser = pw.chromium.launch()
        page = browser.new_context(viewport=VIEWPORT, device_scale_factor=2
                                   ).new_page()

        def shot(path_suffix: str, name: str) -> None:
            page.goto(f"{base}{path_suffix}")
            page.wait_for_load_state("networkidle")
            page.screenshot(path=str(SCREENS / f"{name}.png"))
            print(f"  📸 {name}.png  ({path_suffix})")

        shot("/", "dashboard_busy")
        shot(f"/wo/webapp/{state['wo_rev']['id']}", "wo_detail")
        shot("/neo", "neo_tab")
        shot("/backlog", "backlog")

        # resolve everything the way a user would, then photograph the payoff
        ops.review_work_order(state["wo_rev"]["id"], accept=True)
        ops.neo_answer_escalated(state["q3_id"],
                                 "Yes — approved, $40 is fine for the backfill.")
        from jarvis.central_store import CentralStore
        central = CentralStore()
        try:
            central.ack_inbox()
        finally:
            central.close()
        from jarvis.neo_store import NeoStore
        neo = NeoStore()
        try:
            for q in neo.list_questions(statuses=("answered",),
                                        review_status="unreviewed"):
                neo.review(q["id"], approved=True)
        finally:
            neo.close()
        shot("/", "dashboard_quiet")
        browser.close()
    srv.should_exit = True


if __name__ == "__main__":
    capture()
    print(f"screens in {SCREENS}")
