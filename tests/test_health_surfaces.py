"""A health finding and a remedy on the record and the alarm surfaces (docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md)"""

from __future__ import annotations

import pytest

from jarvis import ops, remedies, supervisor, timeline
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.project_store import NO_TURN, ProjectStore
from jarvis.timeline import build_conversation, build_timeline, event_level

STUCK = "no event, no message and no turn boundary in nineteen hours"
ARGUMENT = "ask it whether the migration script is still running or it has wedged"
NUDGE = "[supervisor] where are you with the migration script?"
NOWHERE = "no fixture anywhere in this module says this sentence"
#: `probes.DEFAULT_PROBES`' first entry, which every project inherits
PROBE, PROBE_TITLE = "no-progress", "Nothing is moving"


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


@pytest.fixture()
def store(started):
    s = ProjectStore(ops.registered_project_paths()["proj_a"])
    try:
        yield s
    finally:
        s.close()


def _client():
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from jarvis.ui.app import create_app
    return TestClient(create_app(), follow_redirects=False)


def ev(kind, ts, **payload):
    return {"kind": kind, "ts": ts, "payload": payload}


def _finding(store, *, flag=True, feature=False, title="the carrier"):
    """One health finding, on a work order or on a feature through its carrier"""
    fo_id = None
    if feature:
        fo = store.create_feature_order("the migration feature")
        store.set_feature_status(fo["id"], "executing")
        fo_id = fo["id"]
    carrier = store.create_work_order(title, parent_id=fo_id,
                                      kind="manager" if feature else "worker",
                                      status="running")["id"]
    alarm = store.add_finding(carrier, kind=PROBE, reason=STUCK, seq=NO_TURN,
                              source="health", probe=PROBE,
                              subject_kind="feature_order" if feature else "work_order",
                              fo_id=fo_id)
    store.add_event(carrier, "health_finding",
                    {"alarm_id": alarm["id"], "probe": PROBE,
                     "subject_kind": "feature_order" if feature else "work_order",
                     "subject_id": fo_id or carrier, "reason": STUCK})
    if flag:
        store.flag_attention(carrier, STUCK)
    return fo_id or carrier, carrier, alarm["id"]


def _propose(store, carrier, alarm_id, *, applied=False):
    """The proposal §5 files, written down rather than judged"""
    approval = store.add_approval(carrier, remedies.GATE_KIND,
                                  f"heal {alarm_id}: nudge — {ARGUMENT}")
    store.update_alarm(alarm_id, status="proposed", verdict="propose",
                       verdict_reason="nineteen hours is past any turn this could be",
                       remedy="nudge", remedy_argument=ARGUMENT,
                       remedy_approval_id=approval["id"])
    store.add_event(carrier, "remedy_proposed",
                    {"alarm_id": alarm_id, "remedy": "nudge",
                     "approval_id": approval["id"], "argument": ARGUMENT})
    if applied:
        store.update_alarm(alarm_id, status="acked")
        store.add_event(carrier, "remedy_applied",
                        {"alarm_id": alarm_id, "remedy": "nudge",
                         "approval_id": approval["id"], "use": 1,
                         "result": f"queued one message on {carrier}"})
    return approval["id"]


def _row(store, alarm_id):
    return store.get_alarm(alarm_id)


# -- the timeline, and the branch that looks fine when it is missing


def test_the_five_new_event_kinds_read_as_five_different_things():
    """`assert event_level("remedy_applied") == "signal"` grades nothing"""
    events = [
        ev("health_finding", 1.0, alarm_id="al-1a2b", probe=PROBE, reason=STUCK),
        ev("health_reviewed", 2.0, subject_kind="work_order", subject_id="wo-1",
           trigger="stale", findings=0),
        ev("remedy_proposed", 3.0, alarm_id="al-1a2b", remedy="nudge",
           approval_id=4, argument=ARGUMENT),
        ev("remedy_applied", 4.0, alarm_id="al-1a2b", remedy="nudge", approval_id=4,
           use=1, result="queued one message on wo-1"),
        ev("remedy_refused", 5.0, alarm_id="al-1a2b", remedy="nudge", approval_id=4,
           verdict="denied", by="neo", reason="the turn is nineteen minutes old"),
    ]

    entries = build_timeline({}, events, [], include_debug=True)

    labels = [e["label"] for e in entries]
    assert len(set(labels)) == 5, labels
    assert all(label and label != entries[i]["kind"]
               for i, label in enumerate(labels)), labels
    assert entries[0]["detail"] == STUCK, "the finding IS the reason"
    assert entries[3]["detail"] == "nudge: queued one message on wo-1"
    assert "denied" not in entries[4]["label"], "the verdict is not the kind"
    assert "refused by neo" in entries[4]["label"]
    # `_ref` resolves off `timeline.ALARM_KINDS` alone and is green with no `_describe` branch at all
    assert [e["ref"] and e["ref"]["id"] for e in entries] == [
        "al-1a2b", None, "al-1a2b", "al-1a2b", "al-1a2b"]
    assert event_level("message_delivered") == "debug"
    assert event_level("health_reviewed") == "debug", "every sweep writes one"


def test_the_supervisors_nudge_is_the_supervisor_speaking_and_not_the_user():
    """`_message_label` names the supervisor, not the user, for its own source"""
    events = [ev("remedy_proposed", 1.0, alarm_id="al-1a2b", remedy="nudge",
                 argument=ARGUMENT)]
    messages = [{"id": 3, "ts": 2.0, "direction": "user_to_agent",
                 "source": remedies.MESSAGE_SOURCE, "content": NUDGE,
                 "status": "delivered"}]

    convo = build_conversation(events, messages)

    assert [(c["kind"], c["who"], c["content"]) for c in convo] == [
        ("message", "supervisor → worker", NUDGE)]
    # The timeline's half of the same source
    labels = [e["label"] for e in build_timeline({}, [], messages)]
    assert labels == ["The supervisor messaged the worker"]


def test_the_two_message_source_literals_agree():
    """`remedies` writes it and `timeline` reads it"""
    assert timeline.SUPERVISOR_SOURCE == remedies.MESSAGE_SOURCE
    assert timeline.SUPERVISOR_SOURCE not in timeline.UNAUTHORED_SOURCES


# -- the memory has to name the probe and the remedy


def test_the_learning_names_the_probe_and_the_remedy_and_leaves_cost_alone():
    """LITERAL EQUALITY, because the existing coverage asserts two substrings are present and a worker who..."""
    reason, why, ruling = STUCK, "it is past any turn", "you called that right"

    cost = supervisor.learning_from_review(
        {"kind": "long-turn", "reason": reason, "verdict": "ack",
         "verdict_reason": why}, ruling)
    health = supervisor.learning_from_review(
        {"kind": PROBE, "probe": PROBE, "source": "health", "reason": reason,
         "verdict": "ack", "verdict_reason": why}, ruling)
    proposed = supervisor.learning_from_review(
        {"kind": PROBE, "probe": PROBE, "source": "health", "reason": reason,
         "verdict": "propose", "verdict_reason": why, "remedy": "nudge",
         "remedy_argument": ARGUMENT}, ruling)

    assert cost == (f"On a long-turn alarm ({reason}) the supervisor decided ack "
                    f"because {why}. The user's ruling: {ruling}")
    assert health == (f"On a no-progress health finding ({reason}) the supervisor "
                      f"decided ack because {why}. The user's ruling: {ruling}")
    assert proposed == (
        f"On a no-progress health finding ({reason}) the supervisor decided propose "
        f"the nudge remedy ({ARGUMENT}) because {why}. The user's ruling: {ruling}")
    # "You were right that it was stuck
    assert len({cost, health, proposed}) == 3


# -- reviewing a proposal is a gate verdict, and the refusal has to say so


def test_a_proposed_alarm_is_refused_with_the_command_that_does_answer_it(store):
    """`review_alarm` already refuses a `proposed` alarm, so asserting the raise grades nothing"""
    _, carrier, alarm_id = _finding(store)
    gate_id = _propose(store, carrier, alarm_id)

    with pytest.raises(ops.OpsError, match="jarvis gate") as raised:
        ops.review_alarm(alarm_id, approved=True, project_name="proj_a")

    assert "proposed" in str(raised.value)
    assert f"jarvis gate approve {gate_id}" in str(raised.value)
    assert _row(store, alarm_id)["review_status"] == "unreviewed"
    assert _row(store, alarm_id)["reviewed_at"] is None
    # The positive partner: the same call on the same alarm once the remedy has been applied goes...
    store.update_alarm(alarm_id, status="acked")
    ops.review_alarm(alarm_id, approved=True, project_name="proj_a")
    assert _row(store, alarm_id)["review_status"] == "approved"


def test_correcting_a_health_finding_teaches_the_supervisor_and_not_neo(store):
    """One learning, seated to the supervisor, naming the probe"""
    from jarvis import neo
    from jarvis.neo_store import SUPERVISOR_SEAT, NeoStore

    _, carrier, corrected = _finding(store)
    _, _, approved = _finding(store, title="another carrier")
    for alarm_id in (corrected, approved):
        store.update_alarm(alarm_id, status="acked", verdict="ack",
                           verdict_reason="it is only warming up")

    neo_store = NeoStore()
    try:
        before = neo.build_system_prompt(neo_store, "proj_a")

        ops.review_alarm(approved, approved=True, project_name="proj_a")
        assert neo_store.learnings(project="proj_a", seat=SUPERVISOR_SEAT) == []

        ops.review_alarm(corrected, approved=False, feedback=NOWHERE,
                         project_name="proj_a")
        rows = neo_store.learnings(project="proj_a", seat=SUPERVISOR_SEAT)
        after = neo.build_system_prompt(neo_store, "proj_a")
    finally:
        neo_store.close()

    assert len(rows) == 1
    assert PROBE in rows[0]["content"] and NOWHERE in rows[0]["content"]
    assert "alarm (" not in rows[0]["content"], "a probe is not a cost alarm kind"
    assert after == before, "the supervisor's memory is not Neo's"


# -- the reads: three halves, no fourth


def _block(page: str, anchor: str) -> str:
    """One alarm's own markup: its anchor up to the next row or the next heading"""
    rest = page[page.index(anchor):]
    ends = [at for at in (rest.find('<div class="msg"', 1), rest.find("<h2>", 1),
                          rest.find("<table", 1)) if at > 0]
    return rest[:min(ends)] if ends else rest


def test_the_page_shows_the_probes_title_and_links_a_feature_finding_at_the_feature(
        store):
    """Two rules in one block, because both are about what the row says it is ABOUT: a kebab id is a..."""
    fo_id, carrier, alarm_id = _finding(store, feature=True)
    store.close()

    page = _client().get("/alarms").text
    block = _block(page, f'id="alarm-{fo_id}"')

    assert PROBE_TITLE in block
    assert PROBE not in block, "the id is a database value, not user copy"
    assert f'href="/fo/proj_a/{fo_id}"' in block
    # No LINK to the carrier: the ack form still posts to it
    assert 'href="/wo/' not in block, "not the carrier"
    assert f'action="/wo/proj_a/{carrier}/ack"' in block
    assert alarm_id in block, "and the alarm is reachable from its own row"


def test_a_remedy_is_on_the_page_for_the_alarm_that_has_one_and_absent_otherwise(
        store):
    """Jinja renders an absent key as the empty string"""
    proposed_subject, carrier, proposed = _finding(store)
    plain_subject, _, plain = _finding(store, title="an untouched order")
    gate_id = _propose(store, carrier, proposed)
    store.close()

    page = _client().get("/alarms").text

    assert ARGUMENT in _block(page, f'id="alarm-{proposed_subject}"')
    assert f'href="/gates/{gate_id}"' in page
    assert "it needs your permission first" in page
    assert ARGUMENT not in _block(page, f'id="alarm-{plain_subject}"')
    assert NOWHERE not in page
    assert plain in page, "and the alarm with no remedy is still on the page"


def test_the_alarms_page_degrades_to_exactly_the_page_that_exists_today(started,
                                                                       monkeypatch,
                                                                       tmp_path):
    """`status_code == 200` proves nothing about "unchanged\""""
    from tests.test_inspection import _burning

    wo_id = _burning(started, monkeypatch, tmp_path, title="a very slow design")

    page = _client().get("/alarms").text

    assert "a very slow design" in page
    assert "still being billed" in page
    assert f'action="/wo/proj_a/{wo_id}/ack"' in page
    assert 'name="back" value="alarms"' in page
    assert "The supervisor asked to" not in page, "no remedy, no remedy block"
    assert "long-turn" in page, "a cost alarm keeps the rendering it has always had"


def test_the_per_alarm_page_carries_the_remedy_and_the_evidence_it_cited(store):
    """The page's one extra over the list"""
    _, carrier, alarm_id = _finding(store)
    approval = store.add_approval(carrier, remedies.GATE_KIND, f"heal {alarm_id}",
                                  evidence=f"# The unit\n{NOWHERE}")
    store.update_alarm(alarm_id, status="proposed", verdict="propose",
                       remedy="nudge", remedy_argument=ARGUMENT,
                       remedy_approval_id=approval["id"])
    store.close()

    page = _client().get(f"/alarms/proj_a/{alarm_id}").text

    assert ARGUMENT in page
    assert NOWHERE in page, "the evidence the finding cited"
    assert f'href="/gates/{approval["id"]}"' in page
    assert f"jarvis gate approve {approval['id']}" in page
    assert 'name="decision"' not in page, \
        "a proposal is answered at the gate, and review_alarm would refuse this"


# -- the CLI is the OS


def test_the_cli_prints_the_same_facts_as_the_two_pages(store, capsys):
    """A feature that exists only on a web page is a bug"""
    from jarvis import cli

    fo_id, carrier, alarm_id = _finding(store, feature=True)
    gate_id = _propose(store, carrier, alarm_id, applied=True)
    store.close()

    assert cli.main(["alarms", "--fo", fo_id]) == 0
    listing = capsys.readouterr().out
    assert PROBE_TITLE in listing and PROBE not in listing
    assert ARGUMENT in listing

    assert cli.main(["alarms", "show", alarm_id]) == 0
    shown = capsys.readouterr().out
    assert PROBE_TITLE in shown
    assert f"gate request #{gate_id}" in shown
    assert f"queued one message on {carrier}" in shown, "what the remedy actually did"

    assert cli.main(["fo", "show", fo_id]) == 0
    feature = capsys.readouterr().out
    assert f"alarms: 1 (1 acked by the supervisor) — {alarm_id}" in feature


def test_jarvis_fo_show_json_carries_the_alarm_rows_in_full(store, capsys):
    """`jarvis wo show --json` carries the `wo_alarms` rows"""
    import json

    from jarvis import cli

    fo_id, _, alarm_id = _finding(store, feature=True)
    store.close()

    assert cli.main(["fo", "show", fo_id, "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)

    assert [a["id"] for a in doc["alarms"]] == [alarm_id]
    assert doc["alarms"][0]["probe"] == PROBE
    assert doc["alarms"][0]["fo_id"] == fo_id
