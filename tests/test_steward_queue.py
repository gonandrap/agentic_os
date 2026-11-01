"""The steward's queue beyond #1186: remedies and Neo riders drain"""

from __future__ import annotations

import json

import pytest

from jarvis import db, ops, remedies, steward
from jarvis.catalog import load_catalog
from jarvis.central_store import CentralStore
from jarvis.daemon import Daemon
from jarvis.project_store import NO_TURN, ProjectStore


@pytest.fixture()
def enforcing(jarvis_home, fake_claude, catalog_file, project, self_dispatch_on):
    """The OS started with the steward enforcing — `weekly_usd` is the room it has left"""
    def make(**evolution):
        data = json.loads(catalog_file.read_text())
        data["os"]["evolution"] = {"mode": "enforce", **evolution}
        catalog_file.write_text(json.dumps(data))
        ops.start_os(str(catalog_file), foreground=True)
        return Daemon(load_catalog(catalog_file))
    return make


@pytest.fixture()
def store(project):
    s = ProjectStore(project)
    yield s
    s.close()


@pytest.fixture()
def central(jarvis_home):
    c = CentralStore()
    yield c
    c.close()


def _drain(central):
    return steward.drain(ops.resolve_catalog(), central)


# ---------------------------------------------------------------- remedies drain


ARGUMENT = "Find what moves the prompt prefix between calls.\nThe read plateau says so."


def _proposed_alarm(store):
    wo = store.create_work_order("the exemplar", status="completed")
    alarm = store.add_finding(wo["id"], kind="rewrite_prefix", reason="prefix churn",
                              seq=NO_TURN, source="cost", remedy="file_work_order",
                              remedy_argument=ARGUMENT, status="proposed")
    return store.get_alarm(alarm["id"]), wo


def _queue_a_remedy(enforcing, store, central):
    enforcing(weekly_usd=0.0)
    alarm, subject = _proposed_alarm(store)
    with pytest.raises(remedies.RemedyRefused, match="steward declined"):
        remedies._apply_file_work_order(store, central, "proj_a", subject,
                                        remedies.Intent.from_alarm(store, alarm))
    [row] = central.steward_decisions()
    return alarm, subject, row


def test_a_queued_remedy_is_kept_and_filed_with_its_ship_order_once_there_is_room(
        enforcing, store, central):
    """One grant use, spent on the refusal: nothing re-asks a queued remedy, so the drain must"""
    alarm, subject, row = _queue_a_remedy(enforcing, store, central)
    assert (row["origin_kind"], row["queue_state"]) == ("remedy", "waiting")
    assert json.loads(row["candidate_json"])["payload"]["alarm_id"] == alarm["id"]
    assert _drain(central)["waiting"] == 1
    assert len(store.list_work_orders()) == 1

    enforcing(weekly_usd=30.0, order_budget_usd=25.0)
    assert _drain(central)["dispatched"] == 1
    filed = [w for w in store.list_work_orders() if w["id"] != subject["id"]]
    fix = next(w for w in filed if not w["title"].startswith("Ship the fix"))
    ship = next(w for w in filed if w["title"].startswith("Ship the fix"))
    assert fix["title"] == "Find what moves the prompt prefix between calls."
    assert alarm["id"] in fix["description"]
    assert store.dependencies(ship) == [fix["id"]]
    for w in (fix, ship):
        assert steward.stamp_of(w)["decision_id"] == row["id"]
        assert w["budget_usd"] == 25.0
    # The alarm is answered: the remedy tick stops re-collecting a spent grant
    assert store.get_alarm(alarm["id"])["status"] == "acked"
    assert [e for e in store.list_events(subject["id"]) if e["kind"] == "remedy_applied"]
    row = central.steward_decision(row["id"])
    assert (row["queue_state"], row["wo_id"], row["applied"]) == ("reoffered", fix["id"], 1)


def test_a_queued_remedy_whose_alarm_was_answered_expires(enforcing, store, central):
    alarm, _subject, row = _queue_a_remedy(enforcing, store, central)
    store.update_alarm(alarm["id"], status="acked")
    enforcing(weekly_usd=30.0)
    assert _drain(central)["expired"] == 1
    assert "already answered" in central.steward_decision(row["id"])["queue_note"]


# ---------------------------------------------------------------- Neo riders drain


def _queue_a_rider(daemon, store):
    wo = store.create_work_order("build the exporter")
    q = {"id": 41, "project": "proj_a", "wo_id": wo["id"]}
    daemon._dispatch_neo_cleanup(store, q, {"dispatch": {
        "title": "supersede the stale gate ruling", "description": "A contradicts B"}})
    return wo


def _cleanups(store):
    return [w for w in store.list_work_orders() if w["origin"] == "neo"]


def test_a_neo_cleanup_files_on_the_first_ask_when_there_is_room(enforcing, store,
                                                                 central):
    """The verdict is the evidence: one sighting meets `min_refs_by_kind.neo_rider`"""
    _queue_a_rider(enforcing(weekly_usd=30.0, order_budget_usd=25.0), store)
    [row] = central.steward_decisions()
    assert (row["origin_kind"], row["outcome"], row["mode"]) == ("neo_rider", "dispatch",
                                                                 "enforce")
    [cleanup] = _cleanups(store)
    assert cleanup["title"] == "supersede the stale gate ruling"
    assert steward.stamp_of(cleanup)["decision_id"] == row["id"]


def test_a_queued_neo_cleanup_is_kept_and_filed_once_the_steward_allows_it(
        enforcing, store, central):
    _queue_a_rider(enforcing(weekly_usd=0.0), store)
    [row] = central.steward_decisions()
    assert (row["origin_kind"], row["queue_state"]) == ("neo_rider", "waiting")
    assert row["rule"] == "ledger"
    assert _cleanups(store) == []

    enforcing(weekly_usd=30.0, order_budget_usd=25.0)
    assert _drain(central)["dispatched"] == 1
    [cleanup] = _cleanups(store)
    assert cleanup["title"] == "supersede the stale gate ruling"
    assert "A contradicts B" in cleanup["description"]
    meta = json.loads(cleanup["metadata"])
    assert meta["pre_approved"]["neo_question_id"] == 41
    assert meta[steward.STAMP_KEY]["decision_id"] == row["id"]
    assert central.steward_decision(row["id"])["wo_id"] == cleanup["id"]


def test_a_neo_cleanup_threshold_raised_back_keeps_it_waiting_on_recurrence(
        enforcing, store, central):
    """The user's config wins over the shipped default: re-decided, never forced"""
    raised = {"min_refs_by_kind": {"neo_rider": 3}}
    _queue_a_rider(enforcing(weekly_usd=30.0, **raised), store)
    [row] = central.steward_decisions()
    assert (row["rule"], row["queue_state"]) == ("recurrence", "waiting")
    assert _drain(central)["waiting"] == 1
    row = central.steward_decision(row["id"])
    assert row["queue_state"] == "waiting" and "(recurrence)" in row["queue_note"]
    assert _cleanups(store) == []


# ---------------------------------------------------------------- precision counts ios


def test_precision_counts_a_stamped_improvement_order_and_its_review(enforcing, store,
                                                                    central):
    enforcing(weekly_usd=100.0)
    cat = ops.resolve_catalog()
    d = steward.gate(steward.Candidate("worker_io", "major", "proj_a", "slow turns",
                                       evidence=("wo-1",), refs=1),
                     catalog=cat, store=store)
    io = store.create_feature_order("slow turns", kind="improvement",
                                    metadata=steward.stamped({}, d), budget_usd=20.0)
    analyst = store.create_work_order("analyse it", parent_id=io["id"], kind="analyst")
    store.update_feature_order(io["id"], plan_wo_id=analyst["id"])
    assert steward.precision(cat)["worker_io"] == (0, 1)
    store.add_event(analyst["id"], "findings_reviewed", {"by": "user"})
    assert steward.precision(cat)["worker_io"] == (1, 1)


def test_a_stamped_order_in_a_stamped_io_is_one_sample_not_two(enforcing, store):
    enforcing(weekly_usd=100.0)
    cat = ops.resolve_catalog()
    d = steward.gate(steward.Candidate("worker_io", "major", "proj_a", "slow turns",
                                       evidence=("wo-1",), refs=1),
                     catalog=cat, store=store)
    io = store.create_feature_order("slow turns", kind="improvement",
                                    metadata=steward.stamped({}, d))
    store.create_work_order("a child", parent_id=io["id"], kind="analyst",
                            metadata=steward.stamped({}, d))
    assert steward.precision(cat)["worker_io"] == (0, 1)


# ---------------------------------------------------------------- legacy rows settle


def _legacy(central, **over):
    return central.record_steward_decision({
        "project": "proj_a", "origin_kind": "panel", "subject": "an old finding",
        "severity": "medium", "outcome": "queue", "rule": "recurrence", "mode": "enforce",
        **over})


def test_a_queue_kept_before_candidates_were_stored_settles_as_expired(jarvis_home,
                                                                       central):
    legacy = _legacy(central)
    no_payload = _legacy(central, origin_kind="remedy",
                         candidate_json=json.dumps({"payload": {}}))
    shadow = _legacy(central, mode="shadow")
    re_asks = _legacy(central, origin_kind="red_main")
    dispatched = _legacy(central, outcome="dispatch", applied=1)
    assert steward.settle_unrecoverable(central) == 2
    for decision_id in (legacy, no_payload):
        row = central.steward_decision(decision_id)
        assert row["queue_state"] == "expired"
        assert row["queue_note"] == steward.UNRECOVERABLE_NOTE
    for decision_id in (shadow, re_asks, dispatched):
        assert central.steward_decision(decision_id)["queue_state"] == ""
    assert steward.settle_unrecoverable(central) == 0
    assert central.steward_queue_counts() == {"expired": 2}


def test_the_drain_settles_legacy_rows_even_with_nothing_waiting(jarvis_home, central,
                                                                 catalog_file):
    ops.start_os(str(catalog_file), foreground=True)
    legacy = _legacy(central)
    assert _drain(central)["expired"] == 1
    assert central.steward_decision(legacy)["queue_state"] == "expired"


def test_a_paused_fleet_still_settles_legacy_rows(monkeypatch, jarvis_home, central):
    from jarvis import daemon, fleet as fleet_mod

    legacy = _legacy(central)
    monkeypatch.setattr(steward, "drain", lambda *a, **k: pytest.fail("drained"))
    monkeypatch.setattr(fleet_mod, "load_pause", lambda c: object())
    stub = type("D", (), {"central": central, "catalog": None})()
    daemon.Daemon.steward_drain(stub)
    assert central.steward_decision(legacy)["queue_state"] == "expired"


# ---------------------------------------------------------------- the dashboard


def test_the_steward_page_shows_counts_then_each_queued_decision(enforcing, store,
                                                                 central):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from jarvis.ui.app import create_app

    _alarm, _subject, row = _queue_a_remedy(enforcing, store, central)
    old = _legacy(central, subject="a finding from before 0.12.15", ts=db.now() - 7200)
    steward.settle_unrecoverable(central)
    html = TestClient(create_app()).get("/steward").text
    assert "1 waiting" in html and "1 expired" in html and "0 re-offered" in html
    waiting, settled = html.split('id="steward-waiting"', 1)[1].split(
        'id="steward-settled"', 1)
    assert f"#{row['id']}" in waiting and f"#{old}" not in waiting
    assert "Find what moves the prompt prefix between calls." in waiting
    assert "a finding from before 0.12.15" in settled
    # The note left the table for the decision's own page (fo-744c42a5 §5)
    assert f'href="/steward/{old}"' in settled
    assert steward.UNRECOVERABLE_NOTE in TestClient(create_app()).get(f"/steward/{old}").text
    # #1185's console: the sidebar link and a `g w` jump; sections stack full width (fo-744c42a5 §2.1)
    assert 'href="/steward"' in html and "w: '/steward'" in html
    assert 'class="flow"' not in html


def test_the_steward_page_lists_an_ask_with_where_to_act(jarvis_home, catalog_file):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from jarvis.ui.app import create_app

    ops.start_os(str(catalog_file), foreground=True)
    central = CentralStore()
    try:
        ask = _legacy(central, origin_kind="morpheus", outcome="ask", severity="critical",
                      rule="critical-ceiling", subject="PR #1241 failed production validation",
                      ts=db.now(), candidate_json=json.dumps({"payload": {"morpheus_item": "mv-1"}}))
    finally:
        central.close()
    html = TestClient(create_app()).get("/steward").text
    asks = html.split('id="steward-asks"', 1)[1].split('id="steward-waiting"', 1)[0]
    # Its morpheus item opens its own detail page (fo-744c42a5 §5), not a scroll on /morpheus
    assert f'id="decision-{ask}"' in asks and 'href="/morpheus/mv-1"' in asks


def test_the_steward_page_renders_with_nothing_queued(jarvis_home, catalog_file):
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from jarvis.ui.app import create_app

    ops.start_os(str(catalog_file), foreground=True)
    resp = TestClient(create_app()).get("/steward")
    assert resp.status_code == 200
    assert "nothing queued" in resp.text


def test_a_decision_row_opens_its_detail_page_with_the_rule(jarvis_home, catalog_file):
    """fo-744c42a5 §5 + Q2441: the NOTE leaves the table for /steward/<order or #n>"""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from jarvis.ui.app import create_app

    ops.start_os(str(catalog_file), foreground=True)
    central = CentralStore()
    try:
        bare = _legacy(central, reason="the weekly ceiling is spent")
        about = _legacy(central, wo_id="wo-abc12345")
        steward.settle_unrecoverable(central)
    finally:
        central.close()
    client = TestClient(create_app())
    page = client.get("/steward").text
    assert f'href="/steward/{bare}"' in page
    assert steward.UNRECOVERABLE_NOTE not in page
    detail = client.get(f"/steward/{bare}").text
    assert steward.UNRECOVERABLE_NOTE in detail and "the weekly ceiling is spent" in detail
    assert f"#{about}" in client.get("/steward/wo-abc12345").text
    assert client.get("/steward/wo-nothing0").status_code == 404
    assert [r["id"] for r in ops.steward_why(f"#{bare}")] == [bare]


def test_a_decision_page_says_whether_it_needs_you(jarvis_home, catalog_file):
    """wo-c9337339: each decision says what, if anything, the user should do"""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from jarvis.ui.app import create_app

    ops.start_os(str(catalog_file), foreground=True)
    central = CentralStore()
    try:
        ask = _legacy(central, origin_kind="morpheus", outcome="ask", severity="critical",
                      rule="critical-ceiling", parent_wo_id="wo-held0001", ts=db.now(),
                      candidate_json=json.dumps({"payload": {"morpheus_item": "mv-1"}}))
        drop = _legacy(central, outcome="drop", rule="duplicate", duplicate_of=ask,
                       parent_wo_id="wo-held0001")
        old = _legacy(central, outcome="ask", ts=db.now() - steward.WINDOW_SECONDS - 60)
        rows = {r["id"]: r for r in central.steward_decisions(limit=50)}
    finally:
        central.close()
    assert steward.next_step(rows[ask])["yours"]
    assert "jarvis morpheus confirm mv-1" in steward.next_step(rows[ask])["text"]
    assert not steward.next_step(rows[drop])["yours"]
    assert f"#{ask}" in steward.next_step(rows[drop])["text"]
    assert not steward.next_step(rows[old])["yours"]
    client = TestClient(create_app())
    page = client.get("/steward/wo-held0001").text
    assert "Needs you:" in page and "what now" in page
    assert "Nothing here needs you." in client.get(f"/steward/{drop}").text
