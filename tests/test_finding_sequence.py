"""An io finding's stated sequence becomes an edge or a refusal (issue 1567)"""

from __future__ import annotations

import pytest

from jarvis import db, invariants, ops, preconditions
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.project_store import ProjectStore
from jarvis.testing import a_report

BRIEF = ("Machine-wide alarm kinds stop naming the latest settled order in rows, timelines "
         "and the inbox, and the inbox body states the action the user owes.")


@pytest.fixture(autouse=True)
def _user_decides(user_decides_findings):
    """The user's review is the path under test"""


@pytest.fixture()
def store(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    Daemon(load_catalog(catalog_file))
    s = ProjectStore(project)
    yield s
    s.close()


@pytest.fixture()
def io_id(store, improvement_order):
    analyst = store.create_work_order("analyse it", description="seen",
                                      kind="planner", status="running")
    store.update_feature_order(improvement_order["id"], plan_wo_id=analyst["id"],
                               status="planning")
    ops.submit_findings(improvement_order["id"], a_report())
    return improvement_order["id"]


def _say(store, io_id, sentence, key="first-turn-reads"):
    report = db.from_json(store.get_feature_order(io_id)["plan"], None)
    finding = next(f for f in report["findings"] if f["key"] == key)
    finding["proposed_orders"][0]["description"] = f"{sentence} {BRIEF}"
    store.update_feature_order(io_id, plan=db.to_json(report))


@pytest.mark.parametrize("text", [
    "Start after PR 1548 merges; read the spec.",
    "- Sequence after other-finding.",
    "This waits for wo-b6a59e82 to land.",
    "Depends on #1548.",
    "Do it once https://github.com/o/r/pull/1548 has merged.",
])
def test_a_stated_sequence_is_detected(text):
    assert preconditions.states_sequence(text)


@pytest.mark.parametrize("text", [
    "After PR 1548 merged, the alarm kept firing.",
    "The description opens 'Start after PR 1548 merges' with no edge.",
    "Wait for CI, then merge.",
    "Start with the store.",
])
def test_history_quotes_and_other_words_are_not_a_sequence(text):
    assert preconditions.states_sequence(text) == []


def test_a_named_pull_request_becomes_an_edge_on_its_owner(store, io_id):
    holder = store.create_work_order("the holder", status="waiting_pr_merge")
    store.conn.execute("UPDATE work_orders SET pr_url=? WHERE id=?",
                       ("https://github.com/o/r/pull/1548", holder["id"]))
    _say(store, io_id, "Start after PR 1548 merges.")

    out = ops.review_findings(io_id, accept=["first-turn-reads"])

    wo = store.get_work_order(out["created"][0]["id"])
    assert store.dependencies(wo) == [holder["id"]]
    assert list(invariants.check_precondition_is_wired(store)) == []


def test_a_sequence_nothing_resolves_is_refused_and_files_nothing(store, io_id):
    _say(store, io_id, "Start after PR 9999 merges.")

    out = ops.review_findings(io_id, accept=["first-turn-reads"])

    assert out["created"] == []
    assert "no order in proj_a owns PR 9999" in out["errors"][0]["error"]


def test_a_filed_sibling_finding_becomes_the_edge(store, io_id):
    report = db.from_json(store.get_feature_order(io_id)["plan"], None)
    report["findings"][1]["proposed_orders"] = [
        {"type": "work", "project": "proj_a", "title": "the prerequisite", "description": BRIEF}]
    store.update_feature_order(io_id, plan=db.to_json(report))
    first = ops.review_findings(io_id, accept=["stale-code-map"])["created"][0]["id"]
    _say(store, io_id, "Sequence after stale-code-map.")

    out = ops.review_findings(io_id, accept=["first-turn-reads"])

    assert store.dependencies(store.get_work_order(out["created"][0]["id"])) == [first]


def test_the_detector_covers_an_unwired_os_filing_only(store):
    typed = store.create_work_order("typed", description="Start after PR 1548 merges.")
    filed = store.create_work_order("filed", description="Start after PR 1548 merges.",
                                    metadata={"origin_io": "io-38a4354f"})

    found = [v.wo_id for v in invariants.check_precondition_is_wired(store)]

    assert found == [filed["id"]] and typed["id"] not in found
