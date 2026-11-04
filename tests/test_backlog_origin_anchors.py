"""An origin link lands on the exact record (2026-11-03-backlog-operability §1.3)"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")

from jarvis import ops  # noqa: E402
from jarvis.project_store import ProjectStore, db  # noqa: E402
from jarvis.testing import a_report, a_verdict  # noqa: E402
from tests.test_ui_debug import client, started  # noqa: E402,F401

REPORT = {
    "summary": "B is cheaper",
    "findings": [{"key": "f1", "title": "cost", "detail": "d", "evidence": ["x"]}],
    "options": [],
    "recommendation": {"option": "b", "why": "cheaper"},
    "proposed_orders": [{"key": "p1", "as": "backlog", "project": "", "title": "tidy",
                         "description": "later"}],
}


@pytest.fixture()
def store(project):
    s = ProjectStore(project)
    yield s
    s.close()


def _lands(client, ref: dict, fragment: str) -> str:  # noqa: F811
    """The href carries the fragment, and the page it reaches holds that element"""
    href, _ = ops.origin_url(ref)
    path, _, got = href.partition("#")
    assert got == fragment
    r = client.get(path, follow_redirects=True)
    assert r.status_code == 200 and "/search" not in str(r.url), r.url
    assert f'id="{fragment}"' in r.text
    return r.text


def _decision(store) -> str:
    do = ops.create_decision_order("proj_a", "which cache", "compare the two caches")
    wo = store.create_work_order("research it", kind="researcher", parent_id=do["id"])
    store.update_feature_order(do["id"], plan_wo_id=wo["id"], plan=db.to_json(REPORT))
    store.set_feature_status(do["id"], "awaiting_decision")
    return do["id"]


def test_envelope_lands_on_the_deferral_and_only_a_deferral_renders(client, store):  # noqa: F811
    wo = store.create_work_order("found something")
    env = store.post_envelope(from_role="implementor", to_role="manager", kind="deferral_request",
                              subject_wo_id=wo["id"],
                              payload={"title": "tidy the parser", "why": "out of scope"})
    other = store.post_envelope(from_role="reviewer", to_role="implementor",
                                kind="review_feedback", subject_wo_id=wo["id"])
    store.mark_envelope(env, "handled_by_router", note="filed backlog item bl-1")
    page = _lands(client, {"kind": "envelope", "project": "proj_a", "id": env,
                           "subject_wo_id": wo["id"]}, f"envelope-{env}")
    assert "tidy the parser" in page and "out of scope" in page and "bl-1" in page
    assert f'id="envelope-{other}"' not in page


def test_a_work_order_with_no_deferral_has_no_deferred_block(client, store):  # noqa: F811
    wo = store.create_work_order("nothing deferred")
    assert "Deferred work" not in client.get(f"/wo/proj_a/{wo['id']}").text


def test_decision_lands_on_the_proposal(client, store):  # noqa: F811
    do_id = _decision(store)
    _lands(client, {"kind": "decision", "do_id": do_id, "key": "p1"}, "proposal-p1")


def test_decision_without_a_key_keeps_the_page_link(client, store):  # noqa: F811
    do_id = _decision(store)
    assert ops.origin_url({"kind": "decision", "do_id": do_id}) == (f"/go/{do_id}", do_id)
    assert client.get(f"/go/{do_id}").headers["location"] == f"/do/proj_a/{do_id}"


def test_finding_lands_on_the_card_of_an_improvement_order(client, store, project):  # noqa: F811
    io = store.create_feature_order("root cause", description="d", kind="improvement")
    analyst = store.create_work_order("analyse it", kind="planner", status="running")
    store.update_feature_order(io["id"], plan_wo_id=analyst["id"], status="planning")
    ops.submit_findings(io["id"], a_report())
    key = ops.show_improvement_order(io["id"])["report"]["findings"][0]["key"]
    _lands(client, {"kind": "finding", "order_id": io["id"], "key": key}, f"finding-{key}")


def test_finding_lands_on_the_row_of_a_decision_order(client, store):  # noqa: F811
    do_id = _decision(store)
    _lands(client, {"kind": "finding", "order_id": do_id, "key": "f1"}, "finding-f1")


def test_finding_of_an_investigation_lands_on_its_verdict(client, store):  # noqa: F811
    inv = store.create_feature_order("why slow", description="d", kind="investigation")
    store.update_feature_order(inv["id"], plan=db.to_json(a_verdict()))
    _lands(client, {"kind": "finding", "order_id": inv["id"], "key": "f1"}, "verdict")


def test_validation_lands_on_the_round(client, store):  # noqa: F811
    wo = store.create_work_order("ship it")
    for _ in range(2):
        opened = store.open_validation_round(wo_id=wo["id"], fingerprint="aaaa1111",
                                             summary="s", evidence="e")
        store.close_validation_round(opened["id"], "rejected", "no test")
    page = _lands(client, {"kind": "validation", "project": "proj_a", "wo_id": wo["id"],
                           "round": 2}, "round-2")
    assert 'id="round-1"' in page


def test_validation_without_a_round_lands_on_the_section(client, store):  # noqa: F811
    wo = store.create_work_order("ship it")
    opened = store.open_validation_round(wo_id=wo["id"], fingerprint="aaaa1111",
                                         summary="s", evidence="e")
    store.close_validation_round(opened["id"], "passed", "fine")
    _lands(client, {"kind": "validation", "project": "proj_a", "wo_id": wo["id"]}, "validation")


def test_a_key_is_escaped_in_the_fragment_and_never_raises():
    href, _ = ops.origin_url({"kind": "finding", "order_id": "io-1", "key": 'a b"#c'})
    assert href == "/go/io-1#finding-a%20b%22%23c"
    href, label = ops.origin_url({"kind": "finding", "order_id": "io-1"})
    assert href == "/search?q=io-1" and label.endswith("(not found)")
