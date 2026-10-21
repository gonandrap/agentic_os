"""Neo files its own pre-approved work order when the ledger contradicts itself"""

from __future__ import annotations

import json

import pytest

from jarvis import db, gates
from jarvis import neo as neo_mod
from jarvis import ops
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.dispatch import build_worker_prompt
from jarvis.project_store import PRE_APPROVED_KEY, ProjectStore


@pytest.fixture()
def gated_catalog(tmp_path, project):
    """A catalog whose only project gates every privileged action"""
    data = {
        "os": {"defaults": {"model": "sonnet"},
               "notifications": {"sinks": ["log"]}},
        "projects": [{"name": "proj_a", "path": str(project),
                      "description": "test project",
                      "gates": {"enabled": list(gates.KIND_NAMES)}}],
    }
    path = tmp_path / "catalog-gated.json"
    path.write_text(json.dumps(data))
    return path


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


@pytest.fixture()
def asked(started):
    """A dispatched work order whose question makes Neo file a cleanup"""
    daemon = started
    wo = ops.create_work_order("proj_a", "build the exporter")
    daemon.tick()
    ops.ask_question(wo["id"], "FORCE_DISPATCH: which ruling applies, the old or the new?")
    daemon._neo_drain()
    return daemon, wo


def work_orders(project) -> list[dict]:
    store = ProjectStore(project)
    try:
        return store.list_work_orders()
    finally:
        store.close()


def cleanup_of(project) -> dict:
    made = [w for w in work_orders(project) if w["origin"] == "neo"]
    assert len(made) == 1, f"expected exactly one Neo-filed work order, got {made}"
    return made[0]


# -- parsing

def test_dispatch_is_absent_from_an_ordinary_verdict() -> None:
    v = neo_mod.parse_verdict('{"escalate": false, "answer": "go", "reason": "r"}')
    assert v["dispatch"] is None


def test_dispatch_is_parsed_when_present() -> None:
    v = neo_mod.parse_verdict(
        '{"escalate": false, "answer": "go", "reason": "r", '
        '"dispatch": {"title": "fix the gate ruling", "description": "A contradicts B"}}')
    assert v["dispatch"] == {"title": "fix the gate ruling", "description": "A contradicts B"}


@pytest.mark.parametrize("blob", [
    '{"escalate": false, "answer": "a", "reason": "r", "dispatch": {"description": "x"}}',
    '{"escalate": false, "answer": "a", "reason": "r", "dispatch": {"title": "  "}}',
    '{"escalate": false, "answer": "a", "reason": "r", "dispatch": "yes please"}',
    '{"escalate": false, "answer": "a", "reason": "r", "dispatch": null}',
])
def test_a_titleless_dispatch_is_dropped(blob: str) -> None:
    """The title is what the user sees in `jarvis wo list`"""
    v = neo_mod.parse_verdict(blob)
    assert v["dispatch"] is None
    assert v["answer"] == "a"


def test_garbage_output_carries_no_dispatch() -> None:
    assert neo_mod.parse_verdict("not json at all")["dispatch"] is None


# -- the daemon acting on it

def test_cleanup_work_order_is_filed(asked, project) -> None:
    cleanup = cleanup_of(project)
    assert cleanup["title"] == "test-forced ledger cleanup"
    assert cleanup["status"] == "pending"
    assert "entries A and B contradict" in cleanup["description"]


def test_the_answer_still_reaches_the_worker(asked, project) -> None:
    """Filing the cleanup is separate work — it must never stand in for answering"""
    daemon, wo = asked
    store = ProjectStore(project)
    try:
        msgs = store.queued_messages(wo["id"])
        assert len(msgs) == 1
        assert msgs[0]["content"].startswith(neo_mod.ANSWER_PREFIX)
    finally:
        store.close()


def test_the_cleanup_says_how_to_correct_an_append_only_store(asked, project) -> None:
    """There is no retraction in either store"""
    description = cleanup_of(project)["description"]
    assert "append-only" in description
    assert "jarvis learn add" in description
    assert "jarvis neo learn" in description


def test_the_cleanup_is_marked_pre_approved(asked, project) -> None:
    marker = db.from_json(cleanup_of(project)["metadata"], {})[PRE_APPROVED_KEY]
    assert marker["by"] == "neo"
    assert marker["scope"]
    assert marker["from_wo"] == asked[1]["id"]


def test_the_origin_work_order_records_it(asked, project) -> None:
    """The user's audit trail: the work order they were watching says where the new one came from"""
    daemon, wo = asked
    store = ProjectStore(project)
    try:
        kinds = [e["kind"] for e in store.list_events(wo["id"])]
        assert "neo_dispatched" in kinds
    finally:
        store.close()


def test_no_dispatch_means_no_work_order(started, project) -> None:
    daemon = started
    wo = ops.create_work_order("proj_a", "ordinary task")
    daemon.tick()
    ops.ask_question(wo["id"], "which linter?")
    daemon._neo_drain()
    assert [w for w in work_orders(project) if w["origin"] == "neo"] == []


def test_an_escalation_can_still_file_the_cleanup(started, project) -> None:
    """Deliberate: a contradicting record is often exactly WHY Neo cannot decide"""
    daemon = started
    wo = ops.create_work_order("proj_a", "risky task")
    daemon.tick()
    ops.ask_question(wo["id"], "FORCE_ESCALATE FORCE_DISPATCH: prod key, and which ruling?")
    daemon._neo_drain()
    assert cleanup_of(project)["title"] == "test-forced ledger cleanup"


def test_a_cleanup_never_files_another_cleanup(started, project) -> None:
    """Neo answers the cleanup worker's questions too"""
    daemon = started
    wo = ops.create_work_order("proj_a", "seed", origin="jarvis")
    daemon.tick()
    ops.ask_question(wo["id"], "FORCE_DISPATCH: which ruling applies?")
    daemon._neo_drain()
    cleanup = cleanup_of(project)

    daemon.tick()  # dispatch the cleanup worker
    ops.ask_question(cleanup["id"], "FORCE_DISPATCH: this record contradicts itself too?")
    daemon._neo_drain()
    assert len([w for w in work_orders(project) if w["origin"] == "neo"]) == 1


def test_gate_reviews_never_dispatch(jarvis_home, fake_claude, gated_catalog,
                                     project) -> None:
    """The reviewer persona knows nothing about cleanups"""
    ops.start_os(str(gated_catalog), foreground=True)
    daemon = Daemon(load_catalog(gated_catalog))
    wo = ops.create_work_order("proj_a", "shipping task")
    daemon.tick()
    ops.request_gate_approval(wo["id"], "./scripts/shipit.sh",
                              why="FORCE_APPROVE FORCE_DISPATCH — tests pass, PR merged",
                              evidence="PR #1 merged")
    daemon._neo_drain()
    store = ProjectStore(project)
    try:
        assert store.list_approvals(wo["id"])[0]["status"] == "approved"
    finally:
        store.close()
    assert [w for w in work_orders(project) if w["origin"] == "neo"] == []


# -- what the cleanup worker is told

def test_the_worker_is_told_not_to_ask_permission(asked, project, catalog_file) -> None:
    cleanup = cleanup_of(project)
    spec = load_catalog(catalog_file).projects[0]
    prompt = build_worker_prompt(cleanup, spec, knowledge=[])
    assert "PRE-APPROVED" in prompt
    assert "do NOT need" in prompt


def test_the_pre_approval_is_scoped_not_blanket(asked, project, catalog_file) -> None:
    """The whole contract above tells the worker to ask on any doubt"""
    cleanup = cleanup_of(project)
    spec = load_catalog(catalog_file).projects[0]
    prompt = build_worker_prompt(cleanup, spec, knowledge=[])
    assert "nothing else" in prompt
    assert "privileged actions are still gated" in prompt.lower()


def test_ordinary_work_orders_get_no_such_briefing(started, project, catalog_file) -> None:
    wo = ops.create_work_order("proj_a", "ordinary task")
    spec = load_catalog(catalog_file).projects[0]
    assert "PRE-APPROVED" not in build_worker_prompt(wo, spec, knowledge=[])


@pytest.mark.parametrize("metadata", [None, "", "not json", "[]", '{"pre_approved": {}}',
                                      '{"pre_approved": "yes"}'])
def test_malformed_metadata_never_breaks_a_briefing(started, project, catalog_file,
                                                    metadata) -> None:
    """A dispatch is the rare path; a briefing that raises would take down every worker launch with it"""
    wo = dict(ops.create_work_order("proj_a", "ordinary task"), metadata=metadata)
    spec = load_catalog(catalog_file).projects[0]
    assert "PRE-APPROVED" not in build_worker_prompt(wo, spec, knowledge=[])


# Written for the opt-in OS (os.autonomy.self_dispatch on, stuck sweep on)
pytestmark = pytest.mark.usefixtures("self_dispatch_on")
