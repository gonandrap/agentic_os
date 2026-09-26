"""Screenshot a `panel_gave_up` hold that NAMES its round, and the same order once a later
round passed, for a PR's UI evidence (kn-c531a831).

docs/superpowers/specs/2026-09-26-a-panel-gave-up-hold-says-which-round-and-stops-when-it-
passes.md. TWO SHOTS, because the change makes two claims and either alone is half of it:

1. the hold says WHICH round gave up and quotes 120 characters of what the panel said,
   with a `round 2 →` link whose target is the anchor the Validation section below emits;
2. once round 3 PASSES the sentence is gone — from the assumption row and from the
   `auto_review:` line — while the event itself stays on the timeline.

`scripts/screenshot_assumption_rulings.py` is the shape this copies (records written
through the stores, so the picture is of a RENDERED record and not of a fake model), and
the rounds are built `scripts/screenshot_forced_round.py`'s way. Everything lives in a temp
`JARVIS_HOME`, so it never reads or writes the live OS:

    uv run python scripts/screenshot_panel_hold_round.py
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
PORT = 8808

JUDGED = "4afb5e7b1c2d3e4f5061728394a5b6c7d8e9f012"

#: What the panel wrote when it gave up. LONGER THAN THE CLAMP on purpose: the hint in the
#: hold has to be seen ending in `…` with the full text one round-link away, which is the
#: whole split the spec chose over putting a paragraph beside an assumption.
PANEL_REASON = (
    "the seats could not agree about whether the retry loop is safe under a partial "
    "write: two read the advisory lock as sufficient and one read it as advisory only, "
    "which is a question about the storage engine rather than about this diff — the chair "
    "would not break the tie, so all three readings come to you"
)


def seed() -> str:
    """One parked work order: an assumption the OS accepted, and one the panel held.

    The hold's TEXT IS NOT TYPED HERE — it is whatever `autoreview.decide` really produces
    for an escalated round 2, so a change that stopped naming the round or stopped quoting
    the reason shows up in the picture instead of being papered over by hand-written prose.
    """
    from jarvis import autoreview
    from jarvis.catalog import ValidationConfig
    from jarvis.central_store import CentralStore
    from jarvis.project_store import ASSUMPTION_DECIDER_OS, ProjectStore

    home = Path(tempfile.mkdtemp())
    proj = home / "jarvis_os"
    proj.mkdir(parents=True)
    catalog = home / "catalog.json"
    catalog.write_text(json.dumps({
        "os": {"defaults": {"model": "opus"}, "ui": {"port": 8787},
               "validation": {"enabled": True}},
        "projects": [{"name": "jarvis_os", "path": str(proj),
                      "description": "the OS itself",
                      "validation": {"auto_review": True}}],
    }, indent=2))
    central = CentralStore()
    central.set_state("catalog_path", str(catalog))
    central.upsert_project("jarvis_os", str(proj), "the OS itself")
    central.close()

    store = ProjectStore(proj)
    wo = store.create_work_order(title="Export the schedule as CSV",
                                 description="one row per shift, ISO dates")
    store.update_work_order(wo["id"], status="needs_review",
                            result_summary="added the exporter and its tests; "
                                           "opened a pull request")
    # An acceptance from an earlier pass, so the `auto_review:` summary has something to
    # fall back TO in the second shot: a line that merely disappeared would not show that
    # the mechanism is still reporting, only that it went quiet.
    decided = store.add_assumption(wo["id"], "named the helper `_render_row`, matching "
                                             "the two beside it")
    store.review_assumption(decided, "accepted", decided_by=ASSUMPTION_DECIDER_OS,
                            reason="a naming convention, not a decision",
                            model="claude-opus-5", config_version="cfg-0007")
    store.add_event(wo["id"], "autoreview_accepted",
                    {"assumption_id": decided, "n": 1, "decided_by": "neo",
                     "model": "claude-opus-5", "neo_question_id": 41,
                     "reason": "a naming convention, not a decision"})

    held = store.add_assumption(wo["id"], "dropped the `--since` flag from the exporter — "
                                          "the spec did not mention it")

    # Round 1 passed on an earlier commit; round 2 is the one that gave up. Two rounds so
    # the link has somewhere to be WRONG: an anchor pointing at the section heading rather
    # than at round 2 is invisible on a page with one round.
    first = store.open_validation_round(wo_id=wo["id"], fingerprint="7c1f9a2e04b3")
    store.set_validation_head(first["id"], JUDGED)
    store.close_validation_round(first["id"], "passed", "")
    second = store.open_validation_round(wo_id=wo["id"], fingerprint="b90d41ee7a15")
    store.set_validation_head(second["id"], JUDGED)
    store.close_validation_round(second["id"], "escalated", PANEL_REASON)
    store.record_validation_opinion(second["id"], "tester", verdict="pass",
                                    reply="The exporter's tests cover the empty case.",
                                    model="claude-opus-5", latency_ms=18400)
    store.record_validation_opinion(second["id"], "security", verdict="reject",
                                    reply="The lock semantics are not mine to judge.",
                                    model="claude-opus-5", latency_ms=16200)

    row = next(a for a in store.all_assumptions(wo["id"]) if a["id"] == held)
    decision = autoreview.decide(
        row, store.get_work_order(wo["id"]),
        ValidationConfig(enabled=True, auto_review=True),
        round_outcome="escalated", round_n=int(second["round"]),
        round_reason=PANEL_REASON)
    assert decision.code == autoreview.HELD_PANEL_GAVE_UP, decision
    store.add_event(wo["id"], "autoreview_held",
                    {"code": decision.code, "reason": decision.reason,
                     "assumption_id": decision.assumption_id, "n": decision.n,
                     "round": decision.round})
    store.flag_attention(wo["id"], "1 assumption pending your review")
    store.close()
    print(f"seeded {wo['id']} — held on round {decision.round}")
    return wo["id"]


def pass_a_later_round(wo_id: str) -> None:
    """Round 3, forced and PASSED at the same commit — wo-15f5d969's real trace."""
    from jarvis.central_store import CentralStore
    from jarvis.project_store import ProjectStore

    central = CentralStore()
    path = Path(central.list_projects()[0]["path"])
    central.close()
    store = ProjectStore(path)
    third = store.open_validation_round(wo_id=wo_id, fingerprint="b90d41ee7a15")
    store.set_validation_head(third["id"], JUDGED)
    store.close_validation_round(third["id"], "passed", "")
    store.record_validation_opinion(third["id"], "tester", verdict="pass",
                                    reply="Same diff, and the seats agreed this time.",
                                    model="claude-opus-5", latency_ms=17100)
    store.close()


def serve() -> None:
    import uvicorn

    from jarvis.ui.app import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="warning")


def shoot(wo_id: str, name: str) -> Path:
    """The work-order page, whole: the hold line and the round it points at are in two
    different sections, and the claim is that the LINK between them resolves."""
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    out = SHOTS / name
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 1100})
        page.goto(f"http://127.0.0.1:{PORT}/wo/jarvis_os/{wo_id}")
        page.wait_for_timeout(200)
        page.screenshot(path=out, full_page=True)
        browser.close()
    return out


def main() -> int:
    os.environ["JARVIS_HOME"] = tempfile.mkdtemp()
    os.environ.pop("JARVIS_WO_ID", None)
    sys.path.insert(0, str(REPO / "src"))
    wo_id = seed()
    threading.Thread(target=serve, daemon=True).start()
    time.sleep(2)
    written = [shoot(wo_id, "panel-hold-names-the-round.png")]
    pass_a_later_round(wo_id)
    written.append(shoot(wo_id, "panel-hold-gone-after-a-later-round-passed.png"))
    print("\n".join(str(q) for q in written))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
