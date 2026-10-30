"""The supervisor hands an alarm it cannot settle to Neo, and Neo hands back advice (docs/superpowers/specs/2026-08-31-the-supervisor.md)"""

# clock-exempt: class 3 — spin-waits for the real supervisor thread to finish draining before an alarm escalates
from __future__ import annotations

import json
import time
from pathlib import Path

import pytest

from jarvis import catalog, ops, supervisor, usage
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.neo_store import NeoStore
from jarvis.project_store import ProjectStore

pytestmark = pytest.mark.usefixtures("neo_projects")

#: Put in a work order's description
ESCALATE = "FORCE_SUPERVISOR_ESCALATE"


# -- fixtures


def _stamp(at: float) -> str:
    from datetime import datetime, timezone
    return datetime.fromtimestamp(at, timezone.utc).isoformat().replace("+00:00", "Z")


def _rows(at: float) -> str:
    """The smallest transcript `inspection.live_alarms` will raise a `long-turn` on"""
    return "".join(json.dumps(r) + "\n" for r in [
        {"type": "user", "timestamp": _stamp(at + 1), "message": {"content": "go"},
         "promptSource": "sdk"},
        {"type": "assistant", "timestamp": _stamp(at + 5),
         "message": {"id": "m1", "model": "claude-opus-5",
                     "usage": {"input_tokens": 0, "cache_creation_input_tokens": 0,
                               "cache_read_input_tokens": 0, "output_tokens": 1},
                     "content": [{"type": "text", "text": "ok"}]}},
    ])


def _enable(catalog_file: Path, **overrides) -> None:
    """The supervisor on, EXPLICITLY, in every test that wants it"""
    data = json.loads(catalog_file.read_text())
    data["os"]["supervisor"] = {"enabled": True, **overrides}
    catalog_file.write_text(json.dumps(data))


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    return lambda: Daemon(load_catalog(catalog_file))


def _burning(daemon, monkeypatch, tmp_path, description: str, status: str = "running"):
    """One work order with a live `long-turn` alarm, raised the way the daemon raises it"""
    root = tmp_path / "projects"
    (root / "-proj").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv(usage.TRANSCRIPT_ROOT_ENV, str(root))
    wo = ops.create_work_order("proj_a", "the long one", description=description)
    store = ProjectStore(ops.find_work_order(wo["id"])[1])
    try:
        turn = store.create_turn(wo["id"], "dispatch", "go")
        at = turn["started_at"]
        (root / "-proj" / f"{wo['id']}.jsonl").write_text(_rows(at))
        store.update_work_order(wo["id"], status="running", session_id=wo["id"])
        with monkeypatch.context() as m:
            m.setattr("jarvis.daemon.time.time", lambda: at + 2 * 3600)
            daemon.check_burning_turns(daemon.catalog.projects[0], store)
        if status != "running":
            store.update_work_order(wo["id"], status=status)
    finally:
        store.close()
    return wo["id"]


def _supervise(daemon, timeout: float = 20.0) -> None:
    """One supervisor tick, waited out on the daemon's OWN guard rather than a sleep"""
    daemon.supervisor_tick()
    deadline = time.monotonic() + timeout
    while daemon.supervisor_draining and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not daemon.supervisor_draining, "the supervisor drain never finished"


def _alarm(wo_id: str) -> dict:
    store = ProjectStore(ops.find_work_order(wo_id)[1])
    try:
        (row,) = store.alarms_of(wo_id)
        return row
    finally:
        store.close()


def _events(wo_id: str, kind: str) -> list[dict]:
    store = ProjectStore(ops.find_work_order(wo_id)[1])
    try:
        return [json.loads(e["payload"]) for e in store.events_of_kind(wo_id, kind)]
    finally:
        store.close()


def _messages(wo_id: str) -> list[dict]:
    store = ProjectStore(ops.find_work_order(wo_id)[1])
    try:
        return store.list_messages(wo_id)
    finally:
        store.close()


def _inbox(wo_id: str) -> list[dict]:
    from jarvis.central_store import CentralStore

    central = CentralStore()
    try:
        return [r for r in central.unacked_inbox() if r["wo_id"] == wo_id]
    finally:
        central.close()


@pytest.fixture()
def escalated(started, catalog_file, monkeypatch, tmp_path):
    """An alarm the supervisor has escalated, with its `alarm` question sitting queued"""
    def go(extra: str = "", status: str = "running"):
        _enable(catalog_file)
        daemon = started()
        wo_id = _burning(daemon, monkeypatch, tmp_path,
                         description=f"{ESCALATE} {extra}".strip(), status=status)
        _supervise(daemon)
        return daemon, wo_id
    return go


# -- the question the supervisor files


def test_the_escalation_becomes_a_neo_question_and_reaches_no_worker(escalated):
    """§3's done condition on the filing side"""
    _daemon, wo_id = escalated()

    alarm = _alarm(wo_id)
    assert alarm["status"] == "escalated"
    assert alarm["verdict"] == "escalate"
    assert alarm["neo_question_id"] is not None

    neo = NeoStore()
    try:
        q = neo.get(alarm["neo_question_id"])
    finally:
        neo.close()
    assert q["kind"] == "alarm"
    assert q["wo_id"] == wo_id and q["project"] == "proj_a"
    assert q["status"] == "queued"

    assert _events(wo_id, "alarm_escalated") == [
        {"alarm_id": alarm["id"], "neo_question_id": alarm["neo_question_id"]}]
    # THE NEGATIVE, and the two rows above are what stop it being vacuous
    assert _messages(wo_id) == []


def test_the_question_carries_the_evidence_packet_and_the_supervisors_reading(escalated):
    """Neo's call is headless and it can look NOTHING up, so a thin context is a thin answer"""
    _daemon, wo_id = escalated()
    alarm = _alarm(wo_id)

    neo = NeoStore()
    try:
        q = neo.get(alarm["neo_question_id"])
    finally:
        neo.close()

    assert "# The alarm" in q["context"]
    assert "# The work order" in q["context"]
    assert "# The session, turn by turn" in q["context"]
    assert "# What the supervisor made of it" in q["context"]
    assert alarm["verdict_reason"] in q["context"]
    assert alarm["reason"] in q["question"] or q["question"].strip()


def test_an_escalation_on_a_settled_order_asks_no_question_and_still_reaches_the_user(
        escalated):
    """Neo can advise nothing about a session that has stopped, so the call is not spent"""
    _daemon, wo_id = escalated(status="completed")

    alarm = _alarm(wo_id)
    assert alarm["status"] == "escalated"
    assert alarm["neo_question_id"] is None

    neo = NeoStore()
    try:
        assert neo.list_questions() == []
    finally:
        neo.close()

    (row,) = _inbox(wo_id)
    assert row["level"] == "warning"
    assert row["title"] == supervisor.ESCALATED_INBOX_TITLE.format(alarm_id=alarm["id"])
    assert f"jarvis alarms show {alarm['id']}" in row["body"]
    assert "completed" in row["body"]


# -- what the drain does with the answer


def test_neos_advice_acks_the_alarm_and_still_reaches_no_worker(escalated):
    """THE BRANCH-ORDER TEST"""
    daemon, wo_id = escalated()
    before = _alarm(wo_id)

    daemon._neo_drain()

    alarm = _alarm(wo_id)
    assert alarm["status"] == "acked"
    assert alarm["verdict"] == "ack"
    assert "Neo" in alarm["verdict_reason"]
    assert alarm["note"] and "test suite" in alarm["note"]

    assert _events(wo_id, "alarm_advice") == [
        {"alarm_id": alarm["id"], "neo_question_id": before["neo_question_id"],
         "answer": alarm["note"]}]
    # The pair
    assert _messages(wo_id) == []
    assert _events(wo_id, "neo_answered") == []


def test_the_advice_reaches_the_user_as_an_inbox_row_pointing_at_the_alarm(escalated):
    """The row the user gets INSTEAD of an attention item"""
    daemon, wo_id = escalated()
    daemon._neo_drain()

    alarm = _alarm(wo_id)
    (row,) = _inbox(wo_id)
    assert row["level"] == "info"
    assert row["title"] == supervisor.ADVICE_INBOX_TITLE.format(wo_id=wo_id)
    assert alarm["note"] in row["body"]
    assert f"/alarms/proj_a/{alarm['id']}" in row["body"]


def test_the_advice_leaves_a_blocker_the_user_never_saw_standing(escalated):
    """`ops.ack_os_flag`, never `ops.ack_attention` — issue 573"""
    daemon, wo_id = escalated()
    store = ProjectStore(ops.find_work_order(wo_id)[1])
    try:
        store.update_work_order(wo_id, status="failed")
        store.flag_attention(wo_id, "worker failed — review and retry")
    finally:
        store.close()

    daemon._neo_drain()

    store = ProjectStore(ops.find_work_order(wo_id)[1])
    try:
        wo = store.get_work_order(wo_id)
        events = store.list_events(wo_id)
    finally:
        store.close()
    assert wo["needs_attention"] == 1
    assert wo["attention_reason"] == "worker failed — review and retry"
    assert wo["acknowledged_blockers"] is None
    assert [e for e in events if e["kind"] == "acknowledged"] == []


def test_the_advice_puts_the_flag_down_when_nothing_else_blocks(escalated):
    """The other half of `ack_os_flag`: with no blocker left the flag it raised goes down"""
    daemon, wo_id = escalated()
    store = ProjectStore(ops.find_work_order(wo_id)[1])
    try:
        store.ack_attention(wo_id, ["something the user really did dismiss"])
        store.flag_attention(wo_id, supervisor.ALARM_BLOCKER.format(
            alarm_id=_alarm(wo_id)["id"]))
    finally:
        store.close()

    daemon._neo_drain()

    store = ProjectStore(ops.find_work_order(wo_id)[1])
    try:
        wo = store.get_work_order(wo_id)
    finally:
        store.close()
    assert wo["needs_attention"] == 0
    assert wo["attention_reason"] is None
    assert json.loads(wo["acknowledged_blockers"]) == [
        "something the user really did dismiss"]


def test_neo_handing_the_alarm_back_leaves_it_escalated_and_flags_the_user(escalated):
    """The other outcome"""
    daemon, wo_id = escalated(extra="FORCE_ALARM_NEO_ESCALATE")
    before = _alarm(wo_id)

    daemon._neo_drain()

    alarm = _alarm(wo_id)
    assert alarm["status"] == "escalated"
    assert alarm["neo_question_id"] == before["neo_question_id"]

    (row,) = _inbox(wo_id)
    assert row["level"] == "warning"
    assert f"jarvis alarms show {alarm['id']}" in row["body"]
    assert f"/neo/question/{alarm['neo_question_id']}" in row["body"]

    store = ProjectStore(ops.find_work_order(wo_id)[1])
    try:
        wo = store.get_work_order(wo_id)
        assert wo["needs_attention"] == 1
        assert alarm["id"] in wo["attention_reason"]
        assert store.list_messages(wo_id) == []
    finally:
        store.close()
    assert _events(wo_id, "alarm_advice") == []


def test_an_alarm_verdict_never_files_a_cleanup_work_order(escalated, project):
    """A cleanup dispatched off a COST OBSERVATION is a work order nobody asked for"""
    daemon, wo_id = escalated(extra="FORCE_DISPATCH")

    store = ProjectStore(project)
    try:
        before = len(store.list_work_orders())
    finally:
        store.close()

    daemon._neo_drain()

    store = ProjectStore(project)
    try:
        after = store.list_work_orders()
    finally:
        store.close()
    assert len(after) == before
    assert [w for w in after if w["origin"] == "neo"] == []
    # The positive partner: the verdict WAS delivered, it just filed nothing
    assert _alarm(wo_id)["status"] == "acked"


def test_a_verdict_for_an_alarm_the_user_already_decided_is_dropped(escalated):
    """`ops.review_alarm` is the close site for the question"""
    daemon, wo_id = escalated()
    alarm_id = _alarm(wo_id)["id"]
    ops.review_alarm(alarm_id, approved=False,
                     feedback="I looked; it is stuck, not slow")

    daemon._neo_drain()

    alarm = _alarm(wo_id)
    assert alarm["status"] == "escalated"      # untouched by Neo
    assert alarm["review_status"] == "corrected"
    assert _events(wo_id, "alarm_advice") == []
    assert _messages(wo_id) == []
    # The positive: the question WAS closed, by the review rather than by the drain
    neo = NeoStore()
    try:
        assert neo.get(alarm["neo_question_id"])["answered_by"] == "os"
    finally:
        neo.close()


def test_the_review_surfaces_light_up_off_the_pointer_this_section_writes(escalated):
    """§5 already reads `neo_advice` through `wo_alarms.neo_question_id`"""
    daemon, wo_id = escalated()
    alarm_id = _alarm(wo_id)["id"]
    assert ops.alarm_detail(alarm_id)["neo_advice"] is None

    daemon._neo_drain()

    detail = ops.alarm_detail(alarm_id)
    assert detail["neo_advice"] == _alarm(wo_id)["note"]
    assert detail["neo_question_status"] == "answered"


# -- `jarvis status` must not offer the wrong command


@pytest.mark.parametrize("kind", ["approval", "plan", "alarm"])
def test_a_question_carried_by_its_own_surface_is_not_offered_to_neo_answer(
        jarvis_home, kind):
    """Each of these is reported by the thing that carries the decision"""
    neo = NeoStore()
    try:
        q = neo.ask("proj_a", "wo-1", "does this need you?", kind=kind)
        neo.mark(q["id"], "escalated", reason="the user must rule")
        control = neo.ask("proj_a", "wo-1", "which library?", kind="question")
        neo.mark(control["id"], "escalated", reason="the user must rule")
    finally:
        neo.close()

    _counts, held = ops._neo_attention()

    assert [h["id"] for h in held] == [control["id"]]


# -- the other door into the worker


def test_answering_an_alarm_question_on_the_neo_surfaces_is_refused(escalated):
    """THE SECOND WAY AN ALARM COULD REACH A WORKER, and it is not through `deliver()`"""
    daemon, wo_id = escalated(extra="FORCE_ALARM_NEO_ESCALATE")
    daemon._neo_drain()
    qid = _alarm(wo_id)["neo_question_id"]
    alarm_id = _alarm(wo_id)["id"]

    with pytest.raises(ops.OpsError, match=f"jarvis alarms review {alarm_id}"):
        ops.neo_answer_escalated(qid, "looks fine to me")

    assert _messages(wo_id) == []
    assert _alarm(wo_id)["status"] == "escalated"
    neo = NeoStore()
    try:
        assert neo.get(qid)["status"] == "escalated"   # not silently half-answered
    finally:
        neo.close()


def test_the_neo_page_sends_an_alarm_question_to_the_alarms_tab(escalated):
    """The template's half of the same guard"""
    pytest.importorskip("fastapi")
    from fastapi.testclient import TestClient

    from jarvis.ui.app import create_app

    daemon, wo_id = escalated(extra="FORCE_ALARM_NEO_ESCALATE")
    daemon._neo_drain()
    qid = _alarm(wo_id)["neo_question_id"]

    page = TestClient(create_app(), follow_redirects=False).get(
        f"/neo/question/{qid}").text

    assert 'action="/neo/%d/answer"' % qid not in page
    assert 'href="/alarms"' in page
    assert "still running" in page


# -- the persona


def test_the_alarm_kind_gets_its_own_persona_and_the_others_are_untouched(jarvis_home):
    """The general answerer persona escalates anything touching production"""
    from jarvis import neo as neo_mod
    from jarvis.gates import REVIEWER_PERSONA
    from jarvis.plans import PLAN_REVIEWER_PERSONA

    store = NeoStore()
    try:
        default = neo_mod.build_system_prompt(store, "proj_a")
        assert default == neo_mod.build_system_prompt(store, "proj_a", kind="question")
        assert default.startswith(neo_mod.PERSONA)

        alarm = neo_mod.build_system_prompt(store, "proj_a", kind="alarm")
        assert alarm.startswith(supervisor.ALARM_REVIEWER_PERSONA)
        assert alarm != default
        assert not alarm.startswith(REVIEWER_PERSONA)
        assert not alarm.startswith(PLAN_REVIEWER_PERSONA)
        # Byte-stable per kind, which is what keeps consecutive alarm reviews on one cached prefix
        assert alarm == neo_mod.build_system_prompt(store, "proj_a", kind="alarm")
    finally:
        store.close()


def test_the_persona_lives_with_the_code_that_files_the_question(jarvis_home):
    """`neo.py` holds no reviewer prose: `REVIEWER_PERSONA` comes from `gates`"""
    source = Path(__import__("jarvis.neo", fromlist=["neo"]).__file__).read_text()

    assert "ALARM_REVIEWER_PERSONA" in source
    assert "You are Neo, reviewing a COST ALARM" not in source


@pytest.mark.parametrize("kind,parent,expected", [
    ("worker", None, "an ordinary work order"),
    ("worker", "fo-1", "a child of feature order fo-1"),
    ("planner", "fo-1", "the PLANNER of feature order fo-1"),
    ("manager", "fo-1", "the MANAGER of feature order fo-1"),
    (None, None, "an ordinary work order"),   # a row written before `kind` existed
])
def test_the_packet_says_which_of_the_three_session_shapes_is_burning(
        kind, parent, expected):
    """An alarm is always raised against a work order"""
    assert expected in supervisor._what_it_is(
        {"kind": kind, "parent_id": parent})
    # Non-vacuous: the three shapes must actually differ from one another
    said = {supervisor._what_it_is({"kind": k, "parent_id": "fo-1"})
            for k in ("worker", "planner", "manager")}
    assert len(said) == 3


def test_both_judges_are_shown_the_session_shape_in_the_same_packet(
        started, monkeypatch, tmp_path):
    """`build_evidence` is the supervisor's prompt AND, forwarded as the escalation context, Neo's"""
    daemon = started()
    wo_id = _burning(daemon, monkeypatch, tmp_path, description="a long brief")
    store = ProjectStore(ops.find_work_order(wo_id)[1])
    try:
        wo = store.get_work_order(wo_id)
        (alarm,) = store.alarms_of(wo_id)
        packet = supervisor.build_evidence(
            store, {"kind": "work_order", "row": wo}, alarm,
            catalog.SupervisorConfig(), daemon.catalog.projects[0].inspect)
    finally:
        store.close()

    assert "this session is an ordinary work order" in packet
    assert packet in supervisor.escalation_context(
        packet, {"reason": "cannot account for it"})
    assert 'READ THE PACKET\'S "this session is" LINE' in \
        supervisor.ALARM_REVIEWER_PERSONA


def test_the_alarm_reviewer_is_told_not_to_escalate_on_spend_alone():
    """The one instruction that separates this persona from the general one"""
    persona = supervisor.ALARM_REVIEWER_PERSONA

    assert "DO NOT ESCALATE MERELY BECAUSE MONEY WAS SPENT" in persona
    assert '"escalate": false' in persona
    assert '"escalate": true' in persona
