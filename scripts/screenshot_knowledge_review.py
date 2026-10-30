"""Screenshot `/knowledge/review` — and drive it: a drag-assign and a drag-link must land in os.db"""

from __future__ import annotations

import argparse
import os
import random
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
SHOTS = REPO / "docs" / "screenshots"
PORT = 8798
TOPICS = ("gates", "spend", "dispatch", "neo", "validation", "knowledge", "ui", "git")


def seed() -> None:
    from jarvis import ops
    from jarvis.central_store import CentralStore

    rnd = random.Random(7)
    c = CentralStore()
    try:
        ids: list[str] = []
        for i in range(140):
            topic = TOPICS[i % len(TOPICS)]
            cite = f" — see {rnd.choice(ids)}" if ids and rnd.random() < 0.3 else ""
            row = c.add_knowledge(f"{topic} lesson {i}: something a worker learned the "
                                  f"hard way about {topic}{cite}", topic=topic,
                                  wo_id=f"wo-{i // 4:08x}")
            ids.append(row["id"])
            for _ in range(rnd.choice((0, 0, 1, 3, 8))):
                c.record_knowledge_read("show", [row], project="jarvis_os", wo_id="wo-x")
    finally:
        c.close()
    # A machine proposal for half the base, one human decision, one nested domain
    by_topic: dict[str, list[str]] = {}
    for n in ops.kb_review_payload()["nodes"]:
        by_topic.setdefault(n["topic"], []).append(n["id"])
    machine = "process:kb-regroup"
    orders = ops.kb_domain_add("Orders", description="work, feature, improvement orders",
                               by=machine)
    for topic in ("gates", "spend", "dispatch"):
        d = ops.kb_domain_add(topic.title(), parent_id=orders["id"] if topic != "spend" else "",
                              by=machine)
        ops.kb_assign(by_topic[topic][: len(by_topic[topic]) // 2 + 4], d["id"], by=machine)
    neo = ops.kb_domain_add("Neo", description="how Neo answers")
    ops.kb_assign(by_topic["neo"][:6], neo["id"])
    ops.kb_link_add(by_topic["neo"][0], "depends_on", by_topic["gates"][0])


def serve() -> None:
    import uvicorn

    from jarvis.ui.app import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="warning")


def drawn(page) -> None:
    page.wait_for_function(
        "window.krCy && !/loading|laying out/.test(document.getElementById('kr-status').textContent)",
        timeout=60_000)
    page.wait_for_timeout(300)


def screen_xy(page, node_id: str) -> tuple[float, float]:
    return tuple(page.evaluate(
        """id => { const n = window.krCy.getElementById(id), p = n.renderedPosition(),
                   b = document.getElementById('cy').getBoundingClientRect();
                   return [b.left + p.x, b.top + p.y]; }""", node_id))


def drag(page, src: tuple[float, float], dst: tuple[float, float]) -> None:
    page.mouse.move(*src)
    page.mouse.down()
    for i in range(1, 16):
        page.mouse.move(src[0] + (dst[0] - src[0]) * i / 15, src[1] + (dst[1] - src[1]) * i / 15)
        page.wait_for_timeout(15)
    page.mouse.up()


def payload(page) -> dict:
    return page.evaluate("fetch('/knowledge/review.json').then(r => r.json())")


def shoot() -> list[Path]:
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    out = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1600, "height": 1000})
        errors: list[str] = []
        page.on("pageerror", lambda e: errors.append(str(e)))
        # Canvas renderer: headless Chromium's WebGL is software (SwiftShader) and too slow to drive
        page.goto(f"http://127.0.0.1:{PORT}/knowledge/review?webgl=0")
        drawn(page)
        out.append(SHOTS / "knowledge-review.png")
        page.screenshot(path=out[-1])

        # Drag-assign: a loose entry onto a leaf domain must land in os.db
        data = payload(page)
        loose = next(n["id"] for n in data["nodes"] if not n["domain_id"])
        domain = page.evaluate(
            "() => window.krCy.nodes('.domain').filter(d => !d.children('.domain').length)[0].id()")
        bb = page.evaluate(
            """id => { const b = window.krCy.getElementById(id).renderedBoundingBox(),
                       c = document.getElementById('cy').getBoundingClientRect();
                       return [c.left + b.x1 + 8, c.top + b.y1 + 22]; }""", domain)
        drag(page, screen_xy(page, loose), tuple(bb))
        page.wait_for_timeout(800)
        after = {n["id"]: n for n in payload(page)["nodes"]}[loose]
        assert after["domain_id"] == domain, f"drag-assign did not land: {after}"
        assert after["assigned_by"] == "human:user", after
        print(f"drag-assign ok: {loose} -> {domain}")

        # Drag-link: link mode, entry to entry, then pick the relationship
        nodes = [n["id"] for n in data["nodes"] if n["id"] != loose]
        a, b = nodes[10], nodes[20]
        page.keyboard.press("l")
        page.evaluate("ids => window.krCy.animate({fit: {eles: window.krCy.getElementById(ids[0])"
                      ".union(window.krCy.getElementById(ids[1])), padding: 200}, duration: 0})",
                      [a, b])
        page.wait_for_timeout(300)
        drag(page, screen_xy(page, a), screen_xy(page, b))
        page.wait_for_selector(".kr-menu")
        page.click(".kr-menu button:has-text('depends on')")
        page.wait_for_timeout(800)
        links = [e for e in payload(page)["edges"] if e["manual"]]
        assert any(e["src"] == a and e["dst"] == b and e["rel"] == "depends_on"
                   for e in links), links
        print(f"drag-link ok: {a} depends_on {b}")
        page.keyboard.press("Escape")

        # 3. the side panel on the entry just assigned
        page.evaluate("() => window.krCy.fit(undefined, 30)")
        page.wait_for_timeout(200)
        page.mouse.click(*screen_xy(page, loose))
        page.wait_for_function("document.getElementById('kr-body') && "
                               "document.getElementById('kr-body').textContent !== 'loading…'")
        out.append(SHOTS / "knowledge-review-panel.png")
        page.screenshot(path=out[-1])
        assert not errors, errors
        browser.close()
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--from-db", help="copy this os.db into the temp home instead of seeding")
    args = ap.parse_args()
    home = Path(tempfile.mkdtemp())
    os.environ["JARVIS_HOME"] = str(home)
    os.environ.pop("JARVIS_WO_ID", None)
    sys.path.insert(0, str(REPO / "src"))
    if args.from_db:
        shutil.copy(args.from_db, home / "os.db")
        from jarvis import ops
        if not ops.kb_review_payload()["domains"]:
            seed_domains_only()
    else:
        seed()
    threading.Thread(target=serve, daemon=True).start()
    time.sleep(2)
    for shot in shoot():
        print(shot)
    return 0


def seed_domains_only() -> None:
    """A real base with no domains yet: a few, so there is something to drop onto"""
    from jarvis import ops

    by_topic: dict[str, list[str]] = {}
    for n in ops.kb_review_payload()["nodes"]:
        by_topic.setdefault(n["topic"] or "(none)", []).append(n["id"])
    for topic, ids in sorted(by_topic.items(), key=lambda t: -len(t[1]))[:5]:
        d = ops.kb_domain_add(topic, by="process:kb-regroup")
        ops.kb_assign(ids, d["id"], by="process:kb-regroup")


if __name__ == "__main__":
    raise SystemExit(main())
