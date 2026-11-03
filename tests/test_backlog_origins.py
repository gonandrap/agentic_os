"""Origin refs: resolver and CLI spelling (2026-11-03-backlog-operability §2.4, §4.2)"""

from __future__ import annotations

import itertools

import pytest

from jarvis import db, ops
from jarvis.central_store import ORIGIN_REF_KINDS, CentralStore
from jarvis.project_store import ProjectStore


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)


@pytest.fixture()
def clock(monkeypatch):
    ticks = itertools.count(1000)
    monkeypatch.setattr(db, "now", lambda: float(next(ticks)))


def test_every_kind_resolves_to_its_page():
    cases = {
        "message": ({"project": "p", "wo_id": "wo-1", "id": 7}, "/wo/p/wo-1#msg-7"),
        "neo_question": ({"id": 12}, "/neo/question/12"),
        "envelope": ({"project": "p", "id": 3, "subject_wo_id": "wo-2"}, "/wo/p/wo-2"),
        "decision": ({"do_id": "do-1", "key": "k1"}, "/go/do-1"),
        "monitoring_order": ({"id": "mo-1"}, "/mo/mo-1"),
        "alarm": ({"project": "p", "id": "al-1"}, "/alarms/p/al-1"),
        "validation": ({"project": "p", "wo_id": "wo-1", "round": 2}, "/wo/p/wo-1#validation"),
        "finding": ({"order_id": "io-1", "key": "f2"}, "/go/io-1"),
        "gate": ({"project": "p", "id": 9}, "/gates/9?project=p"),
        "order": ({"id": "fo-1"}, "/go/fo-1"),
    }
    assert set(cases) | {"turn"} == set(ORIGIN_REF_KINDS)
    for kind, (keys, href) in cases.items():
        got, label = ops.origin_url({"kind": kind, **keys})
        assert got == href, kind
        assert label and "not found" not in label, kind
    assert ops.origin_url({"kind": "decision", "do_id": "do-1", "key": "k1"})[1] == "do-1 k1"


def test_an_unresolvable_ref_never_raises():
    for ref in ({"kind": "nonsense", "id": "x-1"}, {"kind": "message", "id": "m"},
                {"kind": "gate", "project": "p", "id": "not-a-number"}, {}, "junk"):
        href, label = ops.origin_url(ref)  # type: ignore[arg-type]
        assert href.startswith("/search?q=") and label.endswith("(not found)"), ref
    assert ops.origin_url({"kind": "message", "id": "m"})[0] == "/search?q=m"


def test_a_turn_links_the_reply_that_ended_it(started, project, clock):
    store = ProjectStore(project)
    wo = store.create_work_order("running", description="the ask")
    earlier = store.queue_message(wo["id"], "an earlier turn", direction="agent_to_user")
    ref = {"kind": "turn", "project": "proj_a", "wo_id": wo["id"], "ts": db.now()}
    # Still running: the WO page, never the earlier turn's reply
    assert ops.origin_url(ref) == (f"/wo/proj_a/{wo['id']}", f"turn of {wo['id']}")
    reply = store.queue_message(wo["id"], "this turn's reply", direction="agent_to_user")
    store.queue_message(wo["id"], "a later turn", direction="agent_to_user")
    assert reply != earlier
    assert ops.origin_url(ref)[0] == f"/wo/proj_a/{wo['id']}#msg-{reply}"
    missing = {**ref, "wo_id": "wo-00000000"}
    assert ops.origin_url(missing)[1].endswith("(not found)")


def test_list_and_show_carry_resolved_origins(started):
    central = CentralStore()
    try:
        item = central.propose_backlog(
            "proj_a", "a proposal", item_type="bug", severity="major", suggested_kind="wo",
            origin_refs=[{"kind": "neo_question", "id": 4}], origin_wo_id="wo-12345678")
        old = central.add_backlog("proj_a", "older", origin_wo_id="wo-12345678")
    finally:
        central.close()
    shown = ops.show_backlog(item["id"])
    assert shown["origins"] == [{"kind": "neo_question", "href": "/neo/question/4",
                                 "label": "Neo question q4"}]
    assert shown["severity"] == "major" and shown["status"] == "proposed"
    rows = {r["id"]: r for r in ops.list_backlog("proj_a", status=None)}
    assert rows[old["id"]]["origins"] == [{"kind": "order", "href": "/go/wo-12345678",
                                           "label": "wo-12345678"}]
    # The default filter is unchanged: a proposal is not open
    assert [r["id"] for r in ops.list_backlog("proj_a")] == [old["id"]]


def test_parse_origin_ref_spellings(monkeypatch):
    monkeypatch.setenv("JARVIS_WO_ID", "wo-aaaa1111")
    monkeypatch.setenv("JARVIS_PROJECT", "proj_a")
    here = {"project": "proj_a", "wo_id": "wo-aaaa1111"}
    cases = {
        "neo:12": {"kind": "neo_question", "id": 12},
        "q12": {"kind": "neo_question", "id": 12},
        "msg:40": {"kind": "message", **here, "id": 40},
        "mo-abc12345": {"kind": "monitoring_order", "id": "mo-abc12345"},
        "mv-abc12345": {"kind": "monitoring_order", "id": "mv-abc12345"},
        "al-abc12345": {"kind": "alarm", "project": "proj_a", "id": "al-abc12345"},
        "do-abc12345": {"kind": "decision", "do_id": "do-abc12345"},
        "do-abc12345:k2": {"kind": "decision", "do_id": "do-abc12345", "key": "k2"},
        "io-abc12345:f1": {"kind": "finding", "order_id": "io-abc12345", "key": "f1"},
        "inv-abc12345:f1": {"kind": "finding", "order_id": "inv-abc12345", "key": "f1"},
        "gate-7": {"kind": "gate", "project": "proj_a", "id": 7},
        "validation": {"kind": "validation", **here},
        "validation:3": {"kind": "validation", **here, "round": 3},
        "wo-abc12345": {"kind": "order", "id": "wo-abc12345"},
        "fo-abc12345": {"kind": "order", "id": "fo-abc12345"},
    }
    for token, ref in cases.items():
        assert ops.parse_origin_ref(token) == ref, token
    for token in ("bl-abc12345", "io-abc12345", "msg:x", "neo:", "", "gate-x"):
        with pytest.raises(ops.OpsError, match="accepted forms"):
            ops.parse_origin_ref(token)


def test_parse_origin_ref_needs_a_session_for_the_current_wo(monkeypatch):
    monkeypatch.delenv("JARVIS_WO_ID", raising=False)
    monkeypatch.delenv("JARVIS_PROJECT", raising=False)
    for token in ("msg:4", "validation", "al-abc12345", "gate-3"):
        with pytest.raises(ops.OpsError, match="JARVIS_WO_ID"):
            ops.parse_origin_ref(token)
    # Paired: a ref that names no current WO parses outside a session
    assert ops.parse_origin_ref("q4") == {"kind": "neo_question", "id": 4}
