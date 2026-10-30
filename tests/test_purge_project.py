"""Purging a project's own rows from os.db and neo.db (docs/superpowers/specs/2026-10-28-offboard-a-project.md §3)"""

from __future__ import annotations

import inspect

import pytest

from jarvis import db, rules
from jarvis.central_store import PROVENANCE_TABLES, CentralStore
from jarvis.gate_rules import PR_MERGE, RuleSet
from jarvis.neo_store import NeoStore

SCOPE_TABLES = ["agent_calls", "backlog", "inbox", "knowledge", "knowledge_reads",
                "morpheus_items", "rule_fires", "rule_recurrences", "steward_decisions"]
COND = {"field": "status", "op": "eq", "value": "needs_review"}


@pytest.fixture()
def central():
    s = CentralStore()
    yield s
    s.close()


@pytest.fixture()
def neo():
    s = NeoStore()
    yield s
    s.close()


def _fill(central, project):
    kn = central.add_knowledge(f"fact about {project} zanzibar", project=project)
    central.record_knowledge_read("search", [kn], term="zanzibar", project=project)
    central.add_inbox(project, "hello")
    central.add_backlog(project, "an item")
    central.add_agent_call("neo", project=project)
    return kn


def _rows(store, table, project):
    return store.conn.execute(f"SELECT COUNT(*) FROM {table} WHERE project=?",
                              (project,)).fetchone()[0]


def test_neither_purge_takes_a_dry_run():
    for store in (CentralStore, NeoStore):
        assert list(inspect.signature(store.purge_project).parameters) == ["self", "name"]


def test_scope_and_provenance_tables_are_pinned(central):
    derived = db.tables_with_column(central.conn, "project")
    assert sorted(PROVENANCE_TABLES) == ["detectors", "gate_rules"]
    assert sorted(SCOPE_TABLES + list(PROVENANCE_TABLES)) == derived, (
        "a table gained or lost a `project` column: classify it as scope (deleted) or "
        "PROVENANCE_TABLES (never deleted) before purge_project may touch it")
    assert len(derived) == 11


def test_fk_without_target_column_resolves_to_primary_key(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    conn.executescript(
        "CREATE TABLE p (key TEXT PRIMARY KEY, project TEXT);"
        "CREATE TABLE c (pk_ref TEXT REFERENCES p);"
        "INSERT INTO p VALUES ('k1', 'proj_a'), ('k2', 'proj_b');"
        "INSERT INTO c VALUES ('k1'), ('k2');")
    assert db.delete_where(conn, ["p"], "project", "proj_a") == {"c": 1, "p": 1}
    assert [r[0] for r in conn.execute("SELECT pk_ref FROM c")] == ["k2"]


def test_central_purge_leaves_no_scoped_row_and_counts_it(central):
    kn = _fill(central, "proj_a")
    _fill(central, "proj_b")
    hits_before = central.knowledge_hit_counts()
    assert hits_before[kn["id"]] == 1

    counts = central.purge_project("proj_a")

    for table in SCOPE_TABLES:
        assert _rows(central, table, "proj_a") == 0, table
    assert _rows(central, "knowledge", "proj_b") == 1
    assert counts["knowledge"] == counts["inbox"] == counts["knowledge_reads"] == 1
    assert counts["knowledge_read_hits"] == 1
    assert kn["id"] not in central.knowledge_hit_counts()
    # FTS triggers keep search consistent with no index maintenance of ours
    assert central.search_knowledge("zanzibar", project="proj_a") == []
    assert len(central.search_knowledge("zanzibar")) == 1


def test_unknown_project_purges_nothing_and_raises_nothing(central, neo):
    _fill(central, "proj_b")
    assert not any(central.purge_project("nope").values())
    assert not any(neo.purge_project("nope").values())


def test_gate_rule_from_purged_project_still_blocks_others(central):
    rule = central.add_gate_rule("match", "regex", r"\bfrobnicate-ship\b", kind=PR_MERGE,
                                 source="neo", project="proj_a")
    counts = central.purge_project("proj_a")

    assert counts["gate_rules_provenance_cleared"] == 1
    assert central.get_gate_rule(rule["id"])["project"] == ""
    decision = RuleSet.load(central).decide("frobnicate-ship --project proj_b", [PR_MERGE])
    assert decision.match is not None and decision.match.rule_id == rule["id"]


def test_scoped_detector_is_retracted_never_widened(central):
    scoped = central.add_detector("purge-gap", COND, project="proj_a", source="user")
    fleet = central.add_detector("fleet-gap", COND, source="user")
    retired = central.add_detector("old-gap", COND, project="proj_a", source="user")
    central.retract_detector(retired["id"], "earlier")
    central.add_recurrence(gap_class="purge-gap", detector_id=scoped["id"],
                           project="proj_a", order_id="wo-1", verdict=rules.MISSED)

    counts = central.purge_project("proj_a")

    assert counts["detectors_retracted"] == 1 and counts["rule_recurrences"] == 1
    after = central.get_detector(scoped["id"])
    assert after["project"] == "proj_a" and after["retired_at"] is not None
    assert central.get_detector(fleet["id"])["retired_at"] is None
    assert central.get_detector(retired["id"])["retired_reason"] == "earlier"
    assert central.purge_project("proj_a")["detectors_retracted"] == 0


def test_neo_purge_takes_questions_learnings_and_opinions(neo, neo_projects):
    q = neo.ask("proj_a", "wo-1", "which way?")
    neo.record_opinion(q["id"], "premise", reply="left")
    neo.add_learning("prefer left", project="proj_a")
    neo.add_learning("everywhere rule")
    other = neo.ask("proj_b", "wo-2", "which way?")

    counts = neo.purge_project("proj_a")

    assert counts == {"learnings": 1, "panel_opinions": 1, "questions": 1}
    assert _rows(neo, "questions", "proj_a") == _rows(neo, "learnings", "proj_a") == 0
    assert neo.opinions(q["id"]) == []
    assert _rows(neo, "questions", "proj_b") == 1 and other["id"]
    assert _rows(neo, "learnings", "") == 1
