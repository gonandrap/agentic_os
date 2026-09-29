"""Screenshot the spec page and the three links into it, for issue #826's UI evidence.

docs/superpowers/specs/2026-09-28-a-feature-spec-you-can-open.md. The claim a passing
test cannot make: the document the OS HOLDS is readable by a person, before any pull
request exists, and every link that points into it lands on a heading that is there.

Writes PNGs to docs/screenshots/. Everything it touches lives in a temp `JARVIS_HOME`
and a temp catalog, so it never reads or writes the live OS:

    uv run python scripts/screenshot_feature_spec.py
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
PORT = 8807

REPO_PATH = "docs/specs/2026-09-27-self-evolution.md"
SOURCE = "branch wo-1170d758 @ 4f2a1c9"
SECTION = "3"          # the child's section: "## 3. Failure handling"

# A realistic planner-authored document: numbered headings, a fenced bash block whose
# `#` comment must NOT become a heading, a list, bold, inline code and a bare URL.
DOC = """# Self-evolution: the OS files its own repairs

The OS already knows when a post-condition fails. It cannot yet turn that into work.

## 1. The problem

**Every repair starts with a human retyping what the daemon already knows.** The
reconciler names the broken post-condition in `jarvis doctor` output and stops there.
Nothing files the order, so the evidence is copied by hand into a `wo create` and the
link back to the check that fired is lost.

- The check knows the project, the symptom and the last good state.
- The order carries none of it, because a person retyped a sentence.
- Nothing measures how often the same check fires, so no repair is ever prioritised.

## 2. The shape

One reconciler hook, one projection, no new dependency. A failing check emits a
`repair_proposed` event carrying the check id; the daemon turns that into a work order
with the evidence already in the brief.

```bash
# the daemon's own path, run by hand against a temp home
export JARVIS_HOME=$(mktemp -d)
uv run jarvis doctor jarvis_os --repair
uv run jarvis wo list jarvis_os      # the filed repair, with its check id
```

The check id rides on the order as `repair_check`, so `jarvis issues` can count how
many distinct orders one failing check has produced.

## 3. Failure handling

A repair that cannot be filed must be **loud and inert**, never half-filed. Three cases
and one rule: the OS records what it saw and files nothing it cannot explain.

1. The project is not registered — record the check, file nothing, say so in the inbox.
2. An order is already live on the same check — attach the new evidence, file nothing.
3. `ops.create_work_order` raises — the event stays on the timeline with the traceback.

The rule is the same one the gates take: a record the user can read beats an action the
OS cannot defend. Background on the reasoning is at
https://github.com/gonandrap/agentic_os/issues/826 and in the reconciler's docstring.

## 4. Tests

Each case above is pinned by a test that asserts on the RECORD, never on a log line:
`tests/test_self_evolution.py` covers the three failure branches, and one end-to-end
test walks a failing check through to a dispatched order.
"""

PLAN = {
    "summary": "the OS files its own repairs",
    "design_doc": REPO_PATH,
    "design_doc_content": DOC,
    "design_doc_source": SOURCE,
    "justification": ("Two children: the reconciler hook and the failure handling are "
                      "separable, and §3 is the half that must not be rushed."),
    "children": [
        {"key": "hook", "title": "The reconciler hook files a repair",
         "description": ("Emit `repair_proposed` from the failing check and file the "
                         "work order with the evidence already in the brief."),
         "acceptance": "a failing post-condition produces one order carrying its check id",
         "needs": [], "spec_section": "2"},
        {"key": "failures", "title": "A repair that cannot be filed is loud and inert",
         "description": ("The three failure branches of §3: unregistered project, an "
                         "order already live on the same check, and a raising create."),
         "acceptance": "each branch leaves a record and files nothing it cannot explain",
         "needs": ["hook"], "spec_section": SECTION},
    ],
}


def seed() -> tuple[str, str, str]:
    """One planned feature: the planner that wrote the spec, and a child with a section."""
    from jarvis.central_store import CentralStore
    from jarvis.project_store import ProjectStore

    home = Path(tempfile.mkdtemp())
    proj = home / "jarvis_os"
    proj.mkdir(parents=True)
    catalog = home / "catalog.json"
    catalog.write_text(json.dumps({
        "os": {"defaults": {"model": "opus"}, "ui": {"port": 8787}},
        "projects": [{"name": "jarvis_os", "path": str(proj),
                      "description": "the OS itself"}],
    }, indent=2))
    central = CentralStore()
    central.set_state("catalog_path", str(catalog))
    central.upsert_project("jarvis_os", str(proj), "the OS itself")
    central.close()

    store = ProjectStore(proj)
    fo = store.create_feature_order(
        "Self-evolution: the OS files its own repairs",
        description=("When a post-condition fails, file the repair work order from the "
                     "check itself instead of making a person retype the evidence."))
    store.update_feature_order(fo["id"], plan=json.dumps(PLAN))
    store.set_feature_status(fo["id"], "executing")

    planner = store.create_work_order(
        title=f"Plan — {fo['title']}",
        description=("Read the codebase and decompose this feature into work orders. "
                     "You write no product code and open no pull request."),
        kind="planner", parent_id=fo["id"])
    store.update_work_order(planner["id"], session_id="1170d758-plan-self-evolution")
    store.set_status(planner["id"], "completed")

    kid = store.create_work_order(
        title=PLAN["children"][1]["title"],
        description=PLAN["children"][1]["description"],
        kind="worker", parent_id=fo["id"], spec_section=SECTION)
    store.update_work_order(kid["id"], session_id="2b41c9ee-failure-handling")
    store.set_status(kid["id"], "running")
    store.close()
    print(f"seeded {fo['id']}: planner {planner['id']}, child {kid['id']}")
    return str(fo["id"]), str(planner["id"]), str(kid["id"])


def serve() -> None:
    import uvicorn

    from jarvis.ui.app import create_app

    uvicorn.run(create_app(), host="127.0.0.1", port=PORT, log_level="warning")


# Two real pages, side by side in one shot: the link and where it lands. Iframes rather
# than two files because the pair IS the claim — a fragment pointing at no id is the bug.
PAIR = """
<html><body style="margin:0;font:13px system-ui;background:#111;color:#ddd">
<div style="padding:4px 10px">the child's work-order page — the spec line it now carries</div>
<iframe src="{child}" style="width:1280px;height:400px;border:0;background:#fff"></iframe>
<div style="padding:4px 10px">the spec page, at the anchor that link lands on</div>
<iframe src="{spec}" style="width:1280px;height:440px;border:0;background:#fff"></iframe>
</body></html>
"""


def shoot(fo_id: str, planner_id: str, kid_id: str) -> None:
    from playwright.sync_api import sync_playwright

    SHOTS.mkdir(parents=True, exist_ok=True)
    base = f"http://127.0.0.1:{PORT}"
    anchor = "3-failure-handling"
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1280, "height": 900})

        # 1. The document itself, with its provenance line.
        page.goto(f"{base}/spec/jarvis_os/{fo_id}")
        page.screenshot(path=SHOTS / "feature-spec-page.png")

        # 2. The planner's page — the case that had NOTHING before.
        page.goto(f"{base}/wo/jarvis_os/{planner_id}")
        page.screenshot(path=SHOTS / "feature-spec-planner-link.png")

        # 3. The feature page: the spec path is a link, and so is each child's "§ N".
        page.goto(f"{base}/fo/jarvis_os/{fo_id}")
        page.screenshot(path=SHOTS / "feature-spec-feature-page.png")

        # 4. The pair: the child's line, and the heading its fragment resolves to.
        page.set_content(PAIR.format(child=f"{base}/wo/jarvis_os/{kid_id}",
                                     spec=f"{base}/spec/jarvis_os/{fo_id}#{anchor}"))
        page.wait_for_timeout(700)
        # The anchor put at the top of its frame explicitly: a long document scrolled by
        # the fragment alone can land the heading under the frame's own fold.
        page.frames[-1].evaluate(
            "id => { const el = document.getElementById(id);"
            " window.scrollTo(0, el.getBoundingClientRect().top + window.scrollY - 8); }",
            anchor)
        page.wait_for_timeout(300)
        page.screenshot(path=SHOTS / "feature-spec-child-anchor.png")
        browser.close()


def main() -> int:
    os.environ["JARVIS_HOME"] = tempfile.mkdtemp()
    os.environ.pop("JARVIS_WO_ID", None)
    sys.path.insert(0, str(REPO / "src"))
    fo_id, planner_id, kid_id = seed()
    threading.Thread(target=serve, daemon=True).start()
    time.sleep(2)
    shoot(fo_id, planner_id, kid_id)
    for shot in sorted(SHOTS.glob("feature-spec-*.png")):
        print(f"{shot} {shot.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
