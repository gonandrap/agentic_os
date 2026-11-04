"""A GAP verdict's detector and remedy reach the fix order and are checked after it lands (io-38a4354f)"""

from __future__ import annotations

from test_health_sweep import stuck_catalog, stuck_os  # noqa: F401
from test_investigation_orders import (WHY, _investigating, _subject,  # noqa: F401
                                       started, store)

from jarvis import bugreport, db, issues, ops
from jarvis.catalog import load_catalog
from jarvis.testing import a_verdict

NEW_CLASS = "never-registered-mechanism"


def _filed_body(fake_gh) -> str:
    return next(c["stdin"] for c in fake_gh.calls if c["argv"][:2] == ["issue", "create"])


def test_the_filed_issue_carries_the_detector_and_the_remedy(started, store):
    subject = _subject(store)
    inv = ops.create_investigation_order("proj_a", subject, WHY)
    _investigating(store, inv)
    verdict = a_verdict("GAP", subject=subject, gap_class=NEW_CLASS)

    out = ops.submit_verdict(inv["id"], verdict)

    assert out["classification"] == "GAP"
    body = _filed_body(started)
    assert body.startswith("### Acceptance criteria")
    assert verdict["proposed_fix"]["detector"] in body
    assert f"**Remedy:**\n\n{verdict['proposed_fix']['remedy']}" in body
    assert "src/jarvis/gaps.py" in body
    assert bugreport.verdict_gap_class(body) == NEW_CLASS


def test_a_registered_class_is_not_told_to_register(started, store):
    body = bugreport.render_body(description="d", expected="e", actual="a", version="1",
                                 gap_class="stale-hold", detector="det", remedy="none")
    assert "src/jarvis/gaps.py" not in body
    assert bugreport.verdict_gap_class(body) == "stale-hold"


def test_only_a_body_opening_with_the_section_names_a_class():
    plain = bugreport.render_body(description="d", expected="e", actual="a", version="1")
    assert "Acceptance criteria" not in plain
    assert bugreport.verdict_gap_class(plain) == ""
    quoting = bugreport.render_body(
        description="### Acceptance criteria\n\n- **Gap class:** `some-other-class`",
        expected="e", actual="a", version="1")
    assert bugreport.verdict_gap_class(quoting) == ""


def test_the_fix_order_gets_the_class_in_metadata_and_not_the_armed_column(
        started, store, catalog_file):
    spec = load_catalog(catalog_file).project("proj_a")
    body = bugreport.render_body(description="d", expected="e", actual="a", version="1",
                                 gap_class=NEW_CLASS, detector="det", remedy="none")

    url = "https://github.com/x/y/issues/900"
    started.add_issue(url, title="fix it", body=body)

    wo_id = issues.promote_confirmed(
        spec, {"issue_url": url, "title": "fix it", "body": body, "priority": "high"},
        metadata={"investigation": "inv-1"})

    row = store.get_work_order(wo_id)
    metadata = db.from_json(row["metadata"], {})
    assert metadata[bugreport.VERDICT_GAP_CLASS_KEY] == NEW_CLASS
    assert metadata["investigation"] == "inv-1"
    assert not row["gap_class"]
    assert ops.fix_order_precheck(store, spec.path, row) is None, "report, never block"


def _completed_fix(store, gap_class: str) -> str:
    wo_id = store.create_work_order(
        f"fix {gap_class}", metadata={bugreport.VERDICT_GAP_CLASS_KEY: gap_class})["id"]
    store.set_status(wo_id, "completed")
    return wo_id


def _reported(project: str = "proj_a") -> list[dict]:
    doctor = ops.run_doctor(project=project, include_os=False)
    return [v for p in doctor["projects"] for v in p["violations"]
            if v["invariant"] == "INV-FIX-GAP-UNREGISTERED"]


def test_doctor_names_a_completed_fix_whose_class_is_unregistered(started, store):
    landed = _completed_fix(store, NEW_CLASS)
    _completed_fix(store, "stale-hold")
    running = store.create_work_order(
        "still going", metadata={bugreport.VERDICT_GAP_CLASS_KEY: NEW_CLASS})["id"]

    found = _reported()

    assert [v["wo_id"] for v in found] == [landed]
    assert NEW_CLASS in found[0]["detail"] and not found[0]["repaired"]
    assert store.get_work_order(landed)["status"] == "completed"
    assert not store.get_work_order(landed)["needs_attention"], "report, never block"
    assert running not in [v["wo_id"] for v in found]
