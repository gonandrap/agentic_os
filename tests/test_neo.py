"""Neo: worker questions queue → Neo answers as the user (or escalates) →
answers deliver to workers → the user reviews → corrections become learnings."""

from __future__ import annotations

import json

import pytest

from jarvis import neo as neo_mod
from jarvis import ops
from jarvis.catalog import load_catalog
from jarvis.central_store import CentralStore
from jarvis.daemon import Daemon
from jarvis.neo_store import NeoStore
from jarvis.project_store import ProjectStore


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    result = ops.start_os(str(catalog_file), foreground=True)
    assert result["daemon"]["status"] == "foreground"
    return Daemon(load_catalog(catalog_file))


@pytest.fixture()
def asked(started, project):
    """A dispatched work order with one question queued for Neo."""
    daemon = started
    wo = ops.create_work_order("proj_a", "build the exporter")
    daemon.tick()
    result = ops.ask_question(wo["id"], "Should the export default to CSV or JSON?")
    return daemon, wo, result


def drain(daemon):
    """Run the Neo drain synchronously (the daemon thread pool is async)."""
    daemon._neo_drain()


def test_ask_queues_and_parks_worker(asked, project):
    daemon, wo, result = asked
    assert result["question_id"] == 1
    store = ProjectStore(project)
    try:
        fresh = store.get_work_order(wo["id"])
        # parked, but NOT flagged for the user — Neo exists to absorb these
        assert fresh["status"] == "waiting_input"
        assert not fresh["needs_attention"]
        kinds = [e["kind"] for e in store.list_events(wo["id"])]
        assert "question_asked" in kinds
    finally:
        store.close()
    neo = NeoStore()
    try:
        q = neo.get(1)
        assert q["status"] == "queued"
        assert q["project"] == "proj_a"
        assert "build the exporter" in q["context"]
    finally:
        neo.close()


def test_neo_answers_and_delivers_to_worker(asked, project, fake_claude):
    daemon, wo, _ = asked
    drain(daemon)
    neo = NeoStore()
    try:
        q = neo.get(1)
        assert q["status"] == "answered"
        assert q["answered_by"] == "neo"
        assert q["answer"].startswith("neo-decision")
        assert q["review_status"] == "unreviewed"
    finally:
        neo.close()
    # the answer is queued to the worker through the normal delivery path
    store = ProjectStore(project)
    try:
        msgs = store.queued_messages(wo["id"])
        assert len(msgs) == 1
        assert msgs[0]["content"].startswith(neo_mod.ANSWER_PREFIX)
        assert msgs[0]["source"] == "neo"
    finally:
        store.close()
    # the headless call carried the persona as a byte-stable system prompt
    calls = [c for c in fake_claude.calls if "-p" in c["argv"] and "--resume" not in c["argv"]]
    assert len(calls) == 1
    argv = calls[0]["argv"]
    system = argv[argv.index("--append-system-prompt") + 1]
    assert "You are Neo" in system
    assert argv[argv.index("--model") + 1] == "opus"  # catalog default


def test_fifo_order_and_backtoback_drain(started, fake_claude):
    daemon = started
    wo1 = ops.create_work_order("proj_a", "task one")
    wo2 = ops.create_work_order("proj_a", "task two")
    daemon.tick()
    ops.ask_question(wo1["id"], "first question")
    ops.ask_question(wo2["id"], "second question")
    ops.ask_question(wo1["id"], "third question")
    drain(daemon)
    calls = [c for c in fake_claude.calls if "-p" in c["argv"] and "--resume" not in c["argv"]]
    prompts = [c["argv"][c["argv"].index("-p") + 1] for c in calls]
    assert [p.splitlines()[-1] for p in prompts] == [
        "first question", "second question", "third question"]
    # cache economics: identical system prompt bytes across the whole drain
    systems = {c["argv"][c["argv"].index("--append-system-prompt") + 1] for c in calls}
    assert len(systems) == 1


def test_escalation_reaches_the_user(asked, project):
    daemon, wo, _ = asked
    ops.ask_question(wo["id"], "FORCE_ESCALATE: may I rotate the production key?")
    drain(daemon)
    neo = NeoStore()
    try:
        q2 = neo.get(2)
        assert q2["status"] == "escalated"
    finally:
        neo.close()
    # escalations DO demand the user: inbox item + wo attention + status listing
    central = CentralStore()
    try:
        items = central.unacked_inbox()
        assert any("Neo escalated" in i["title"] for i in items)
    finally:
        central.close()
    store = ProjectStore(project)
    try:
        assert store.get_work_order(wo["id"])["needs_attention"]
    finally:
        store.close()
    st = ops.os_status()
    assert any(a["status"] == "neo_escalated" for a in st["attention"])
    assert st["neo"]["escalated"] == 1


def test_garbage_output_escalates_not_delivers(asked, project):
    """Unparseable model output must never reach a worker as an answer."""
    daemon, wo, _ = asked
    ops.ask_question(wo["id"], "FORCE_GARBAGE: what about the schema?")
    drain(daemon)
    neo = NeoStore()
    try:
        q2 = neo.get(2)
        assert q2["status"] == "escalated"
        assert "unparseable" in q2["answer_reason"]
    finally:
        neo.close()


def test_user_answers_escalated_question(asked, project):
    daemon, wo, _ = asked
    ops.ask_question(wo["id"], "FORCE_ESCALATE: prod decision")
    drain(daemon)
    result = ops.neo_answer_escalated(2, "Yes, rotate it during the maintenance window")
    assert result["delivery"]["wo_id"] == wo["id"]
    neo = NeoStore()
    try:
        q = neo.get(2)
        assert q["status"] == "answered"
        assert q["answered_by"] == "user"
        assert q["review_status"] == "approved"  # user-authored, nothing to review
    finally:
        neo.close()
    store = ProjectStore(project)
    try:
        contents = [m["content"] for m in store.queued_messages(wo["id"])]
        assert any("[Answer from the user]" in c for c in contents)
    finally:
        store.close()
    # answering resolves the escalation: it leaves the attention list
    st = ops.os_status()
    assert not any(a["status"] == "neo_escalated" for a in st["attention"])


def test_review_approve(asked):
    daemon, wo, _ = asked
    drain(daemon)
    result = ops.neo_review(1, approved=True)
    assert result["review"] == "approved"
    assert not result["learning_recorded"]
    neo = NeoStore()
    try:
        assert neo.get(1)["review_status"] == "approved"
        assert neo.counts()["unreviewed"] == 0
    finally:
        neo.close()


def test_correction_becomes_learning_and_reaches_worker(asked, project):
    daemon, wo, _ = asked
    drain(daemon)
    result = ops.neo_review(1, approved=False,
                            feedback="Always default to CSV; JSON only behind a flag")
    assert result["review"] == "corrected"
    assert result["learning_recorded"]
    assert result["forwarded_to_worker"]
    neo = NeoStore()
    try:
        learnings = neo.learnings("proj_a")
        assert len(learnings) == 1
        assert "Always default to CSV" in learnings[0]["content"]
        assert learnings[0]["source"] == "review"
    finally:
        neo.close()
    store = ProjectStore(project)
    try:
        contents = [m["content"] for m in store.queued_messages(wo["id"])]
        assert any("Correction from the user" in c for c in contents)
    finally:
        store.close()


def test_correction_requires_feedback(asked):
    daemon, wo, _ = asked
    drain(daemon)
    with pytest.raises(ops.OpsError):
        ops.neo_review(1, approved=False, feedback="   ")


def test_learnings_shape_future_answers(asked, fake_claude):
    """The feedback loop: a correction appears in Neo's next system prompt, and the
    prompt grows append-only so the previously cached prefix stays valid."""
    daemon, wo, _ = asked
    drain(daemon)
    calls = [c for c in fake_claude.calls if "-p" in c["argv"] and "--resume" not in c["argv"]]
    system_before = calls[-1]["argv"][calls[-1]["argv"].index("--append-system-prompt") + 1]

    ops.neo_review(1, approved=False, feedback="Prefer CSV, always")
    ops.ask_question(wo["id"], "And what delimiter?")
    drain(daemon)
    calls = [c for c in fake_claude.calls if "-p" in c["argv"] and "--resume" not in c["argv"]]
    system_after = calls[-1]["argv"][calls[-1]["argv"].index("--append-system-prompt") + 1]
    assert "Prefer CSV, always" in system_after
    # append-only: the old prefix (minus the placeholder line) survives verbatim
    head = system_before.replace("(none yet — escalate when unsure)\n", "").rstrip()
    assert system_after.startswith(head.split("# Learnings")[0])
    assert "# Learnings" in system_after


def test_neo_disabled_via_catalog(jarvis_home, fake_claude, tmp_path, project, claude_json):
    data = {
        "os": {"neo": {"enabled": False}},
        "projects": [{"name": "proj_a", "path": str(project)}],
    }
    path = tmp_path / "catalog-noneo.json"
    path.write_text(json.dumps(data))
    ops.start_os(str(path), foreground=True)
    daemon = Daemon(load_catalog(path))
    wo = ops.create_work_order("proj_a", "task")
    daemon.tick()
    ops.ask_question(wo["id"], "anyone home?")
    daemon.neo_tick()
    assert not daemon.neo_draining
    neo = NeoStore()
    try:
        assert neo.get(1)["status"] == "queued"  # untouched — Neo is off
    finally:
        neo.close()


def test_daemon_tick_triggers_drain(asked):
    """The real path: tick() notices queued questions and drains on the neo thread."""
    daemon, wo, _ = asked
    daemon.neo_tick()
    daemon.neo_pool.shutdown(wait=True)  # join the drain thread
    neo = NeoStore()
    try:
        assert neo.get(1)["status"] == "answered"
    finally:
        neo.close()


def test_parse_verdict_tolerates_fences():
    v = neo_mod.parse_verdict('```json\n{"escalate": false, "answer": "go", "reason": "r"}\n```')
    assert v == {"escalate": False, "answer": "go", "reason": "r"}
    v = neo_mod.parse_verdict("total nonsense")
    assert v["escalate"] is True
