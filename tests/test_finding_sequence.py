"""An io finding's stated sequence becomes an edge or a refusal (issue 1567)"""

from __future__ import annotations

import pytest

from jarvis import db, invariants, ops, preconditions
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.project_store import DEPENDENCY_DEAD_STATUSES, ProjectStore
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


def test_a_sibling_accepted_in_the_same_call_files_first(store, io_id):
    report = db.from_json(store.get_feature_order(io_id)["plan"], None)
    report["findings"][1]["proposed_orders"] = [
        {"type": "work", "project": "proj_a", "title": "the prerequisite", "description": BRIEF}]
    store.update_feature_order(io_id, plan=db.to_json(report))
    _say(store, io_id, "Sequence after stale-code-map.")

    out = ops.review_findings(io_id, accept=["first-turn-reads", "stale-code-map"])

    assert out["errors"] == []
    prerequisite, waiter = out["created"][0]["id"], out["created"][1]["id"]
    assert store.dependencies(store.get_work_order(waiter)) == [prerequisite]


def _edit(store, io_id, **fields):
    """Set fields on the finding under review and on its first proposed order"""
    report = db.from_json(store.get_feature_order(io_id)["plan"], None)
    finding = next(f for f in report["findings"] if f["key"] == "first-turn-reads")
    if "recommendation" in fields:
        finding["recommendation"] = fields.pop("recommendation")
    finding["proposed_orders"][0].update(fields)
    store.update_feature_order(io_id, plan=db.to_json(report))


def _refused(store, io_id, clause):
    out = ops.review_findings(io_id, accept=["first-turn-reads"])
    assert out["created"] == []
    assert clause in out["errors"][0]["error"]
    return out["errors"][0]["error"]


def test_a_sequence_only_in_the_recommendation_becomes_the_edge(store, io_id):
    holder = store.create_work_order("the holder")
    _edit(store, io_id, recommendation=f"Start after {holder['id']} merges.")

    out = ops.review_findings(io_id, accept=["first-turn-reads"])

    assert store.dependencies(store.get_work_order(out["created"][0]["id"])) == [holder["id"]]


def test_a_sequence_only_in_the_recommendation_is_refused_when_nothing_resolves(store, io_id):
    _edit(store, io_id, recommendation="Start after PR 9999 merges.")

    _refused(store, io_id, "Start after PR 9999 merges.")


def test_a_named_work_order_becomes_the_edge(store, io_id):
    holder = store.create_work_order("the holder")
    _say(store, io_id, f"This waits for {holder['id']} to land.")

    out = ops.review_findings(io_id, accept=["first-turn-reads"])

    assert store.dependencies(store.get_work_order(out["created"][0]["id"])) == [holder["id"]]


def test_a_named_io_finding_with_a_filed_order_becomes_the_edge(store, io_id):
    holder = store.create_work_order("the holder")
    other = store.create_feature_order("other", description="seen", kind="improvement")
    store.update_feature_order(other["id"], plan=db.to_json({"findings": [
        {"key": "theirs", "created_orders": [
            {"index": 0, "id": holder["id"], "type": "work", "project": "proj_a"}]}]}))
    _say(store, io_id, f"Depends on {other['id']}.")

    out = ops.review_findings(io_id, accept=["first-turn-reads"])

    assert store.dependencies(store.get_work_order(out["created"][0]["id"])) == [holder["id"]]


def test_a_named_io_finding_with_no_filed_order_is_refused(store, io_id):
    other = store.create_feature_order("other", description="seen", kind="improvement")
    store.update_feature_order(other["id"], plan=db.to_json({"findings": [{"key": "theirs"}]}))
    _say(store, io_id, f"Depends on {other['id']}.")

    error = _refused(store, io_id, f"Depends on {other['id']}.")

    assert f"{other['id']} has no order filed in proj_a" in error


def test_a_named_backlog_item_becomes_an_open_precondition_row(store, io_id):
    _say(store, io_id, "Depends on bl-d5baa28b.")

    out = ops.review_findings(io_id, accept=["first-turn-reads"])

    wo_id = out["created"][0]["id"]
    assert [r["ref"] for r in store.open_preconditions(wo_id)] == ["bl-d5baa28b"]
    assert store.dependencies(store.get_work_order(wo_id)) == []


@pytest.mark.parametrize("status", DEPENDENCY_DEAD_STATUSES)
def test_a_dead_holder_is_refused(store, io_id, status):
    holder = store.create_work_order("the holder", status=status)
    _say(store, io_id, f"Depends on {holder['id']}.")

    error = _refused(store, io_id, f"Depends on {holder['id']}.")

    assert f"{holder['id']} is {status}" in error


def test_a_dead_sibling_order_is_refused(store, io_id):
    report = db.from_json(store.get_feature_order(io_id)["plan"], None)
    report["findings"][1]["proposed_orders"] = [
        {"type": "work", "project": "proj_a", "title": "the prerequisite", "description": BRIEF}]
    store.update_feature_order(io_id, plan=db.to_json(report))
    first = ops.review_findings(io_id, accept=["stale-code-map"])["created"][0]["id"]
    store.conn.execute("UPDATE work_orders SET status='cancelled' WHERE id=?", (first,))
    _say(store, io_id, "Sequence after stale-code-map.")

    error = _refused(store, io_id, "Sequence after stale-code-map.")

    assert f"{first} is cancelled" in error


def test_a_pull_request_with_a_dead_newer_owner_edges_on_the_live_one(store, io_id):
    url = "https://github.com/o/r/pull/1548"
    live = store.create_work_order("the live owner", status="waiting_pr_merge")
    dead = store.create_work_order("the dead owner", status="cancelled")
    store.conn.execute("UPDATE work_orders SET pr_url=? WHERE id IN (?, ?)",
                       (url, live["id"], dead["id"]))
    store.conn.execute("UPDATE work_orders SET created_at=created_at+100 WHERE id=?",
                       (dead["id"],))
    _say(store, io_id, "Start after PR 1548 merges.")

    out = ops.review_findings(io_id, accept=["first-turn-reads"])

    assert store.dependencies(store.get_work_order(out["created"][0]["id"])) == [live["id"]]


def test_a_sequence_on_a_feature_order_is_refused(store, io_id):
    _edit(store, io_id, type="feature", description=f"Start after PR 1548 merges. {BRIEF}")

    error = _refused(store, io_id, "Start after PR 1548 merges.")

    assert "a feature order takes no dependency edge" in error


def test_a_stop_phrase_the_wider_wording_misses_is_refused(store, io_id):
    _say(store, io_id, "Do not start until the migration lands.")

    assert preconditions.states_sequence("Do not start until the migration lands.")
    _refused(store, io_id, "Do not start until the migration lands.")


def test_a_ref_past_the_quoted_length_still_resolves(store, io_id):
    holder = store.create_work_order("the holder")
    padding = "x" * preconditions.MAX_SENTENCE
    _say(store, io_id, f"Start after {padding} and {holder['id']} completes.")

    out = ops.review_findings(io_id, accept=["first-turn-reads"])

    assert store.dependencies(store.get_work_order(out["created"][0]["id"])) == [holder["id"]]


def test_the_user_can_reject_a_refused_finding(store, io_id):
    _say(store, io_id, "Start after reading the spec.")
    _refused(store, io_id, "Start after reading the spec.")

    ops.review_findings(io_id, reject={"first-turn-reads": "misread: no prerequisite here"})

    report = db.from_json(store.get_feature_order(io_id)["plan"], None)
    finding = next(f for f in report["findings"] if f["key"] == "first-turn-reads")
    assert finding["status"] == "rejected" and "filing_error" not in finding


def test_neo_cannot_reject_a_finding_already_accepted(store, io_id):
    _say(store, io_id, "Start after reading the spec.")
    _refused(store, io_id, "Start after reading the spec.")

    with pytest.raises(ops.OpsError, match="a decision is taken once"):
        ops.review_findings(io_id, reject={"first-turn-reads": "no"}, decided_by="neo")


def test_the_detector_covers_an_unwired_os_filing_only(store):
    typed = store.create_work_order("typed", description="Start after PR 1548 merges.")
    filed = store.create_work_order("filed", description="Start after PR 1548 merges.",
                                    metadata={"origin_io": "io-38a4354f"})

    found = [v.wo_id for v in invariants.check_precondition_is_wired(store)]

    assert found == [filed["id"]] and typed["id"] not in found
