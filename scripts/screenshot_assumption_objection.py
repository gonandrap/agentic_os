"""Screenshot every end state of an objection on one page, for a PR's UI evidence (§8).

The claim a passing UI test cannot make: a reader can tell an objection still in flight,
one delivered and unanswered, one the worker answered, one withdrawn because the order
stopped, and one that was NEVER DELIVERED apart — beside a historical assumption that
grows no extra line at all, which is the contrast asserting one string cannot show.

`scripts/screenshot_assumption_rulings.py` is the shape this copies, including the temp
`JARVIS_HOME` that keeps it off the live OS:

    uv run python scripts/screenshot_assumption_objection.py
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
PORT = 8811
SHOT = "wo-assumption-objection.png"


def seed() -> None:
    """One work order carrying all four objection end states plus the two untouched rows.

    Written through the stores rather than driven through a daemon, for
    `screenshot_auto_review.py`'s reason: the picture is of a RENDERED record, and a Neo
    drain would make it a picture of the fake model.

    Timestamps are set explicitly because `ops.objection_response` reads FORWARD from
    each delivery over the whole order's acts: row 2's "waiting on the worker" only holds
    while no act of any other row falls after its delivery.
    """
    from jarvis.project_store import ASSUMPTION_DECIDER_OS, ProjectStore

    from jarvis.central_store import CentralStore

    home = Path(tempfile.mkdtemp())
    proj = home / "jarvis_os"
    proj.mkdir(parents=True)
    catalog = home / "catalog.json"
    catalog.write_text(json.dumps({
        "os": {"defaults": {"model": "opus"}, "ui": {"port": 8787},
               "validation": {"enabled": True}},
        "projects": [{"name": "jarvis_os", "path": str(proj),
                      "description": "the OS itself",
                      "validation": {"auto_review": True, "early_review": True}}],
    }, indent=2))
    central = CentralStore()
    central.set_state("catalog_path", str(catalog))
    central.upsert_project("jarvis_os", str(proj), "the OS itself")
    central.close()

    now = time.time()
    t0 = now - 3 * 3600
    store = ProjectStore(proj)
    wo = store.create_work_order(title="Retry the webhook delivery",
                                 description="back off exponentially, cap at 5 attempts")
    store.set_status(wo["id"], "running")

    def assumption(content: str, offset: float) -> int:
        aid = store.add_assumption(wo["id"], content)
        store.conn.execute("UPDATE assumptions SET ts=? WHERE id=?", (t0 + offset, aid))
        return aid

    def objected(aid: int, reason: str, offset: float, *, transport: str = "queue",
                 envelope_state: str = "queued", msg_status: str | None = None) -> int:
        store.record_provisional(aid, verdict="object", reason=reason, model="opus",
                                 stakes="routine")
        # `record_provisional` stamps wall clock; the reading has to precede the send it
        # caused, or the picture shows an objection posted before it was formed.
        store.conn.execute("UPDATE assumptions SET provisional_ts=? WHERE id=?",
                           (t0 + offset - 60, aid))
        env_id = store.post_envelope(from_role="reviewer", to_role="implementor",
                                     kind="assumption_objection",
                                     payload={"reason": reason},
                                     subject_wo_id=wo["id"])
        store.record_objection(aid, envelope_id=env_id, transport=transport,
                               sent_ts=t0 + offset)
        if msg_status is not None:
            msg_id = store.queue_message(wo["id"], f"the OS objects: {reason}",
                                         source="bus")
            store.mark_message(msg_id, msg_status)
            store.conn.execute("UPDATE envelopes SET delivered_msg_id=? WHERE id=?",
                               (msg_id, env_id))
        if envelope_state != "queued":
            store.mark_envelope(env_id, envelope_state)
        return env_id

    # 1. in flight: envelope queued, neither delivered nor withdrawn.
    flight = assumption("retry on any 5xx, including 501", 0)
    objected(flight, "501 means the endpoint will never accept it — retrying is a loop",
             5 * 60)

    # 2. delivered, unanswered. Its delivery is a minute ago, AFTER every other act on
    # the order, which is what leaves "waiting on the worker since …" true.
    waiting = assumption("wrote the attempt counter to the shared Redis key", 10 * 60)
    objected(waiting, "that key is read by the scheduler; a second writer races it",
             12 * 60, transport="peer")
    store.mark_objection_delivered(waiting, now - 60)

    # 3. delivered and ANSWERED — the worker's next assumption (#4) is the act read back.
    answered = assumption("capped the backoff at 60s", 20 * 60)
    objected(answered, "the spec says 5 attempts, and 60s caps it at 5 minutes", 22 * 60)
    store.mark_objection_delivered(answered, t0 + 25 * 60)

    # 4. withdrawn under §6.6: the order stopped before the queue got to it.
    withdrawn = assumption("moved the counter to a per-endpoint key instead", 30 * 60)
    env = objected(withdrawn, "a per-endpoint key changes the metrics schema",
                   32 * 60, envelope_state="withdrawn")
    store.withdraw_objection(withdrawn, t0 + 35 * 60)
    store.add_event(wo["id"], "autoreview_objection_withdrawn",
                    {"assumption_id": withdrawn, "n": 4, "envelope_id": env,
                     "reason": "the order stopped before the objection was delivered"})

    # 5. UNDELIVERABLE (§9): the carrier gave up — the message row is `failed`.
    dead = assumption("dropped the dead-letter queue entirely", 45 * 60)
    objected(dead, "the dead-letter queue is how a lost delivery is ever found again",
             47 * 60, envelope_state="undeliverable", msg_status="failed")

    # 6. THE DISCRIMINATING ROW: every early-pass column empty, so no extra line at all.
    # A renderer that labelled every assumption would pass every other assertion here.
    assumption("put the new test beside the module's other tests", 50 * 60)

    # 7. Settled, and its early reading STAYS (§7): the provisional accept and the
    # confirmation read together, one badge and one "provisionally" beside it.
    confirmed = assumption("named the helper `_next_delay`, matching the two beside it",
                           55 * 60)
    store.record_provisional(confirmed, verdict="accept", model="opus", stakes="routine",
                             reason="a naming convention, not a decision")
    store.conn.execute("UPDATE assumptions SET provisional_ts=? WHERE id=?",
                       (t0 + 56 * 60, confirmed))
    store.review_assumption(confirmed, "accepted", decided_by=ASSUMPTION_DECIDER_OS,
                            reason="confirmed against the diff — the helper is named as "
                                   "the early pass read it",
                            model="claude-opus-5", config_version="cfg-0007")
    store.add_event(wo["id"], "autoreview_confirmed",
                    {"assumption_id": confirmed, "n": 7, "model": "claude-opus-5",
                     "reason": "confirmed against the diff"})

    store.flag_attention(wo["id"], "an objection was never delivered to the worker")
    store.close()
    print(f"seeded {wo['id']}")


def serve() -> None:
    import uvicorn

    from jarvis.ui.app import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="warning")


def shoot() -> None:
    from playwright.sync_api import sync_playwright

    from jarvis.central_store import CentralStore
    from jarvis.project_store import ProjectStore

    central = CentralStore()
    path = Path(central.list_projects()[0]["path"])
    central.close()
    store = ProjectStore(path)
    wo_id = store.list_work_orders()[0]["id"]
    store.close()

    SHOTS.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 1400})
        page.goto(f"http://127.0.0.1:{PORT}/wo/jarvis_os/{wo_id}")
        page.screenshot(path=SHOTS / SHOT)
        browser.close()


def main() -> int:
    os.environ["JARVIS_HOME"] = tempfile.mkdtemp()
    os.environ.pop("JARVIS_WO_ID", None)
    sys.path.insert(0, str(REPO / "src"))
    seed()
    threading.Thread(target=serve, daemon=True).start()
    time.sleep(2)
    shoot()
    print(SHOTS / SHOT)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
