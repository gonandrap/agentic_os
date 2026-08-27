"""The knowledge base is measured, not assumed.

`test_knowledge_ondemand.py` pins what a worker prompt COSTS. This pins the other half,
which did not exist: what is actually READ. The OS could say what it knew and nothing at
all about whether anyone consulted it — the only evidence was an opt-in paid eval
somebody had to remember to run — so an entry nobody has ever opened and a work order
that never looked were both invisible.

Two properties carry most of these tests. Recording NEVER breaks the read it counts, and
a sweep of the index (`list`, `topics`) is not a retrieval: counting it as one would mark
the whole base consulted and destroy the only number that says which entries earn their
place.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from jarvis import ops
from jarvis.central_store import AIMED_VERBS, CentralStore
from jarvis.cli import build_parser, cmd_learn
from jarvis.project_store import ProjectStore


def run(*argv: str) -> int:
    return cmd_learn(build_parser().parse_args(["learn", *argv]))


@pytest.fixture()
def base(jarvis_home):
    central = CentralStore()
    central.add_knowledge("DEPLOY RUNS FROM THE TAG, never the branch. " + "x" * 400,
                          project="p1", topic="releases")
    central.add_knowledge("THE GATE MATCHES BYTE FOR BYTE.", project="p1", topic="gates")
    yield central
    central.close()


# -- the read log ---------------------------------------------------------------------

def test_show_records_the_entry_it_returned(base, capsys, monkeypatch):
    kid = base.search_knowledge("DEPLOY")[0]["id"]
    monkeypatch.setenv("JARVIS_WO_ID", "wo-1")
    monkeypatch.setenv("JARVIS_PROJECT", "p1")
    run("show", kid)
    capsys.readouterr()

    read = base.knowledge_reads()[0]
    assert (read["verb"], read["wo_id"], read["project"]) == ("show", "wo-1", "p1")
    assert read["hits"] == 1 and read["chars"] > 400
    assert base.knowledge_hit_counts() == {kid: 1}


def test_a_read_with_no_work_order_is_a_person_at_a_terminal(base, capsys, monkeypatch):
    monkeypatch.delenv("JARVIS_WO_ID", raising=False)
    monkeypatch.delenv("JARVIS_PROJECT", raising=False)
    run("search", "gate")
    capsys.readouterr()

    assert base.knowledge_reads()[0]["wo_id"] == ""
    assert base.knowledge_read_summary()["by_workers"] == 0


def test_a_search_that_finds_nothing_is_recorded_as_asked_and_unanswered(base, capsys):
    run("search", "kubernetes")
    capsys.readouterr()

    summary = base.knowledge_read_summary()
    assert summary["reads"] == 1 and summary["misses"] == 1
    assert summary["unanswered"][0]["term"] == "kubernetes"


def test_listing_the_index_is_not_a_retrieval(base, capsys):
    """`list` sweeps; only `show`/`search` aim. Crediting a sweep to every entry it
    printed would make "never read" unreachable."""
    run("list")
    capsys.readouterr()

    assert base.knowledge_reads()[0]["verb"] == "list"
    assert base.knowledge_hit_counts() == {}
    assert "list" not in AIMED_VERBS


def test_a_headline_list_is_not_charged_for_bodies_it_never_printed(base, capsys):
    run("list")
    headlines = base.knowledge_reads()[0]["chars"]
    run("list", "--full")
    full = base.knowledge_reads()[0]["chars"]
    capsys.readouterr()
    assert 0 < headlines < full


def test_topics_is_recorded_without_touching_any_entry(base, capsys):
    run("topics")
    capsys.readouterr()
    read = base.knowledge_reads()[0]
    assert (read["verb"], read["hits"], read["chars"]) == ("topics", 2, 0)


def test_recording_never_breaks_the_read_it_counts(base, capsys, monkeypatch):
    """An observer that can fail the thing it observes is worse than no observer."""
    base.conn.execute("DROP TABLE knowledge_reads")
    assert run("search", "gate") == 0
    assert "GATE MATCHES" in capsys.readouterr().out


def test_deleting_a_work_order_takes_its_reads_with_it(base, capsys, monkeypatch):
    monkeypatch.setenv("JARVIS_WO_ID", "wo-gone")
    run("search", "gate")
    capsys.readouterr()
    assert base.purge_work_order("wo-gone")["knowledge_reads"] == 1
    assert base.knowledge_reads() == []
    assert base.knowledge_hit_counts() == {}


# -- the report -----------------------------------------------------------------------

@pytest.fixture()
def fleet(jarvis_home, project):
    """A registered project with a completed work order and two entries."""
    central = CentralStore()
    central.upsert_project("proj_a", str(project))
    yield central
    central.close()


def _completed(project: Path, wo_id: str, title: str) -> None:
    store = ProjectStore(project)
    try:
        store.create_work_order(wo_id=wo_id, title=title, description="")
        store.update_work_order(wo_id, status="completed")
    finally:
        store.close()


def test_the_prompt_cost_is_measured_against_a_real_prompt(fleet):
    fleet.add_knowledge("A DEPLOY LESSON. " + "x" * 300, project="proj_a",
                        topic="releases")
    cost = ops.knowledge_usage_report(project="proj_a")["prompt_cost"][0]
    assert cost["indexed"] == 1
    # The index ships and the body does not: the block is a fraction of the prompt while
    # the entry itself is longer than the block.
    assert 0 < cost["index_chars"] < cost["prompt_chars"]
    assert cost["body_chars"] > 300


def test_an_entry_nobody_has_opened_is_named(fleet, capsys):
    fleet.add_knowledge("NEVER OPENED.", project="proj_a", topic="t")
    report = ops.knowledge_usage_report(project="proj_a")
    assert report["never_read_count"] == 1
    assert report["never_read"][0]["headline"] == "NEVER OPENED."

    run("search", "NEVER")
    capsys.readouterr()
    assert ops.knowledge_usage_report(project="proj_a")["never_read_count"] == 0


def test_an_order_that_finished_without_reading_is_counted(fleet, project):
    fleet.record_knowledge_read("search", term="anything")  # the log begins
    _completed(project, "wo-silent", "rewrite the release runbook")
    report = ops.knowledge_usage_report(project="proj_a")
    assert report["silent_order_count"] == 1
    assert report["silent_orders"][0]["wo_id"] == "wo-silent"


def test_work_that_predates_the_log_is_not_accused_of_ignoring_the_base(fleet, project):
    """Reads were unrecorded until this table existed. Reporting every order that
    finished before then as one that never consulted memory would turn a missing
    measurement into a finding — 125 of them, on the fleet this shipped to."""
    _completed(project, "wo-ancient", "rewrite the release runbook")
    report = ops.knowledge_usage_report(project="proj_a")
    assert report["observed_from"] is None
    assert report["silent_order_count"] == 0

    fleet.record_knowledge_read("search", term="anything")
    _completed(project, "wo-modern", "rewrite the release runbook")
    report = ops.knowledge_usage_report(project="proj_a")
    assert [o["wo_id"] for o in report["silent_orders"]] == ["wo-modern"]


def test_could_have_read_needs_an_entry_that_predates_the_order(fleet, project):
    """The signal is 'the answer was already there'. An entry the order wrote ITSELF is
    not something it failed to read, and counting it would flag every order that
    recorded a learning."""
    fleet.record_knowledge_read("search", term="anything")  # the log begins
    _completed(project, "wo-late", "rewrite the release runbook")
    fleet.add_knowledge("THE RELEASE RUNBOOK rewrite lesson.", project="proj_a",
                        topic="releases")
    assert ops.knowledge_usage_report(project="proj_a")["could_have_read_count"] == 0

    fleet.conn.execute("UPDATE knowledge SET ts=1")  # recorded long before the order
    report = ops.knowledge_usage_report(project="proj_a")
    assert report["could_have_read_count"] == 1
    assert report["could_have_read"][0]["entries"][0]["headline"].startswith("THE RELEASE")


def test_an_order_that_read_is_not_reported_as_silent(fleet, project, capsys,
                                                      monkeypatch):
    fleet.add_knowledge("SOMETHING TO FIND.", project="proj_a", topic="t")
    _completed(project, "wo-read", "rewrite the release runbook")
    monkeypatch.setenv("JARVIS_WO_ID", "wo-read")
    monkeypatch.setenv("JARVIS_PROJECT", "proj_a")  # the pair dispatch sets
    run("search", "SOMETHING")
    capsys.readouterr()

    report = ops.knowledge_usage_report(project="proj_a")
    assert report["silent_order_count"] == 0
    assert report["reads"]["orders"] == 1


def test_truncated_headlines_are_counted(fleet):
    """The index line is the only thing that decides whether an entry is ever read, so
    how many entries reach it cut mid-sentence is a first-class number."""
    fleet.add_knowledge("SHORT ONE.", project="proj_a", topic="t")
    fleet.add_knowledge("A HEADLINE THAT RUNS ON. " * 20, project="proj_a", topic="t")
    assert ops.knowledge_usage_report(project="proj_a")["size"]["truncated_headlines"] == 1


def test_stats_renders_without_a_read_in_the_log(fleet, capsys):
    fleet.add_knowledge("ANYTHING.", project="proj_a", topic="t")
    assert run("stats", "--project", "proj_a") == 0
    out = capsys.readouterr().out
    assert "WHAT IT COSTS A PROMPT" in out and "ORDERS THAT NEVER LOOKED" in out
