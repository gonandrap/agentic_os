"""Neo behavioral scorecard (deterministic).

What Neo PROMISES: questions never get lost, answers reach the right worker,
escalations always reach the user, corrections always become learnings, and the
token bill stays flat because the prompt prefix is byte-stable and FIFO-drained.
"""

from __future__ import annotations

import pytest

from jarvis import neo as neo_mod
from jarvis import ops
from jarvis.catalog import load_catalog
from jarvis.central_store import CentralStore
from jarvis.daemon import Daemon
from jarvis.neo_store import NeoStore
from jarvis.project_store import ProjectStore

scenario = pytest.mark.scenario


@pytest.fixture()
def daemon(jarvis_home, fake_claude, catalog_file):
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


def dispatched_wo(daemon, title="eval task"):
    wo = ops.create_work_order("proj_a", title)
    daemon.tick()
    return wo


def neo_calls(fake_claude):
    return [c for c in fake_claude.calls
            if "-p" in c["argv"] and "--resume" not in c["argv"]]


def system_of(call):
    return call["argv"][call["argv"].index("--append-system-prompt") + 1]


def question_of(call):
    return call["argv"][call["argv"].index("-p") + 1].splitlines()[-1]


# -- 1. no question is ever lost ---------------------------------------------------

OUTCOME_BATTERY = [
    ("plain question gets answered and delivered", "which linter?", "answered"),
    ("model-declined question escalates", "FORCE_ESCALATE: delete prod db?", "escalated"),
    ("unparseable model output escalates, never delivers", "FORCE_GARBAGE: schema?", "escalated"),
    ("failed model call surfaces as failed, not silence", "FORCE_FAIL: anyone?", "failed"),
]


@scenario("neo/no-question-lost", "every question reaches a terminal, visible state")
@pytest.mark.parametrize("name,question,expected", OUTCOME_BATTERY,
                         ids=[c[0] for c in OUTCOME_BATTERY])
def test_question_outcomes(daemon, project, name, question, expected):
    wo = dispatched_wo(daemon)
    qid = ops.ask_question(wo["id"], question)["question_id"]
    daemon._neo_drain()
    neo = NeoStore()
    try:
        q = neo.get(qid)
        assert q["status"] == expected
    finally:
        neo.close()
    store = ProjectStore(project)
    try:
        delivered = [m for m in store.queued_messages(wo["id"])
                     if m["content"].startswith(neo_mod.ANSWER_PREFIX)]
        if expected == "answered":
            assert len(delivered) == 1, "answer must be queued to the worker"
        else:
            assert not delivered, "non-answers must never reach the worker"
            # …and the user must be able to see it
            st = ops.os_status()
            assert any(a.get("neo_question_id") == qid for a in st["attention"])
    finally:
        store.close()


@scenario("neo/no-question-lost", "asking parks the worker quietly (no user attention)")
def test_ask_is_quiet(daemon, project):
    wo = dispatched_wo(daemon)
    ops.ask_question(wo["id"], "a benign question")
    store = ProjectStore(project)
    try:
        fresh = store.get_work_order(wo["id"])
        assert fresh["status"] == "waiting_input" and not fresh["needs_attention"]
    finally:
        store.close()
    assert not ops.os_status()["attention"]


# -- 2. escalations demand the user everywhere ------------------------------------

@scenario("neo/escalation-surfacing", "escalation hits inbox + wo attention + status + counts")
def test_escalation_surfaces_everywhere(daemon, project):
    wo = dispatched_wo(daemon)
    qid = ops.ask_question(wo["id"], "FORCE_ESCALATE: rotate the key?")["question_id"]
    daemon._neo_drain()
    central = CentralStore()
    try:
        assert any("Neo escalated" in i["title"] and i["wo_id"] == wo["id"]
                   for i in central.unacked_inbox())
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
    assert not st["healthy"]


@scenario("neo/escalation-surfacing", "user's answer resolves the escalation end to end")
def test_user_answer_resolves(daemon, project):
    wo = dispatched_wo(daemon)
    qid = ops.ask_question(wo["id"], "FORCE_ESCALATE: prod?")["question_id"]
    daemon._neo_drain()
    ops.neo_answer_escalated(qid, "No — wait for Friday's window.")
    st = ops.os_status()
    assert not any(a["status"] == "neo_escalated" for a in st["attention"])
    store = ProjectStore(project)
    try:
        assert any("Friday's window" in m["content"]
                   for m in store.queued_messages(wo["id"]))
    finally:
        store.close()


# -- 3. token economics: stable prefix, FIFO, back-to-back --------------------------

@scenario("neo/token-economics", "system prompt is byte-identical across a 6-question drain")
def test_prefix_stability_across_drain(daemon, fake_claude):
    wo = dispatched_wo(daemon)
    for i in range(6):
        ops.ask_question(wo["id"], f"question number {i}")
    daemon._neo_drain()
    systems = {system_of(c) for c in neo_calls(fake_claude)}
    assert len(systems) == 1, "any byte drift kills the shared cache prefix"


@scenario("neo/token-economics", "questions answered strictly FIFO")
def test_fifo(daemon, fake_claude):
    wo = dispatched_wo(daemon)
    order = [f"q-{i}" for i in range(8)]
    for q in order:
        ops.ask_question(wo["id"], q)
    daemon._neo_drain()
    assert [question_of(c) for c in neo_calls(fake_claude)] == order


@scenario("neo/token-economics", "question content stays out of the shared prefix")
def test_question_not_in_system(daemon, fake_claude):
    wo = dispatched_wo(daemon)
    ops.ask_question(wo["id"], "UNIQUE_MARKER_XYZZY should not leak into the prefix")
    daemon._neo_drain()
    assert "UNIQUE_MARKER_XYZZY" not in system_of(neo_calls(fake_claude)[-1])


@scenario("neo/token-economics", "a new learning extends the prefix append-only")
def test_append_only_prefix(jarvis_home):
    neo = NeoStore()
    try:
        neo.add_learning("first rule", project="")
        before = neo_mod.build_system_prompt(neo, "proj_a")
        neo.add_learning("second rule", project="")
        after = neo_mod.build_system_prompt(neo, "proj_a")
        assert after.startswith(before), "prefix must only grow, never rewrite"
    finally:
        neo.close()


# -- 4. the learning loop ------------------------------------------------------------

@scenario("neo/learning-loop", "correction → learning → next prompt → worker guidance")
def test_correction_loop(daemon, project, fake_claude):
    wo = dispatched_wo(daemon)
    qid = ops.ask_question(wo["id"], "tabs or spaces?")["question_id"]
    daemon._neo_drain()
    ops.neo_review(qid, approved=False, feedback="Spaces, 4, no debate.")
    # (a) learning recorded
    neo = NeoStore()
    try:
        assert any("Spaces, 4" in l["content"] for l in neo.learnings("proj_a"))
    finally:
        neo.close()
    # (b) worker gets the correction
    store = ProjectStore(project)
    try:
        assert any("Correction from the user" in m["content"]
                   for m in store.queued_messages(wo["id"]))
    finally:
        store.close()
    # (c) the next answer sees it
    ops.ask_question(wo["id"], "and for YAML files?")
    daemon._neo_drain()
    assert "Spaces, 4" in system_of(neo_calls(fake_claude)[-1])


@scenario("neo/learning-loop", "approval confirms without polluting learnings")
def test_approval_no_learning(daemon):
    wo = dispatched_wo(daemon)
    qid = ops.ask_question(wo["id"], "which test runner?")["question_id"]
    daemon._neo_drain()
    ops.neo_review(qid, approved=True)
    neo = NeoStore()
    try:
        assert neo.learnings("proj_a") == []
        assert neo.counts()["unreviewed"] == 0
    finally:
        neo.close()


@scenario("neo/learning-loop", "project-scoped learnings don't bleed across projects")
def test_learning_scoping(jarvis_home):
    neo = NeoStore()
    try:
        neo.add_learning("proj_a only: use tabs", project="proj_a")
        neo.add_learning("everywhere: be terse", project="")
        a = neo_mod.build_system_prompt(neo, "proj_a")
        b = neo_mod.build_system_prompt(neo, "proj_b")
        assert "use tabs" in a and "use tabs" not in b
        assert "be terse" in a and "be terse" in b
    finally:
        neo.close()


@scenario("neo/learning-loop", "review is mandatory bookkeeping: unreviewed count is exact")
def test_unreviewed_accounting(daemon):
    wo = dispatched_wo(daemon)
    for i in range(3):
        ops.ask_question(wo["id"], f"choice {i}?")
    daemon._neo_drain()
    assert ops.os_status()["neo"]["unreviewed"] == 3
    ops.neo_review(1, approved=True)
    ops.neo_review(2, approved=False, feedback="wrong, do X")
    assert ops.os_status()["neo"]["unreviewed"] == 1
