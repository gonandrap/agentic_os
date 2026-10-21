"""What a dependency ACTUALLY shipped, in the dispatched prompt"""

from __future__ import annotations

import pytest

from jarvis import dispatch
from jarvis.catalog import load_catalog
from jarvis.project_store import ProjectStore

HEADING = "# What your dependencies ACTUALLY shipped"


@pytest.fixture()
def store(project):
    s = ProjectStore(project)
    yield s
    s.close()


@pytest.fixture()
def spec(catalog_file):
    return load_catalog(catalog_file).projects[0]


def _dep(store, *, title="build the exporter", summary="added Exporter.run()",
         pr="https://github.com/x/y/pull/7", status="completed"):
    dep = store.create_work_order(title, "d")
    store.update_work_order(dep["id"], status=status, result_summary=summary,
                            pr_url=pr)
    return store.get_work_order(dep["id"])


def _child(store, dep_ids, *, kind="worker"):
    return store.create_work_order("use the exporter", "the dependency added Exporter.run()",
                                   depends_on=list(dep_ids), kind=kind)


def _prompt(store, spec, wo):
    return dispatch.build_worker_prompt(
        wo, spec, None, dependencies=dispatch.dependency_context(store, wo))


# -- the block

def test_a_completed_dependency_puts_its_result_and_pr_in_the_prompt(store, spec):
    dep = _dep(store)
    prompt = _prompt(store, spec, _child(store, [dep["id"]]))

    assert HEADING in prompt
    assert dep["id"] in prompt
    assert "added Exporter.run()" in prompt
    assert "https://github.com/x/y/pull/7" in prompt
    assert "[completed]" in prompt


def test_the_block_says_the_brief_is_a_hint_and_the_record_wins(store, spec):
    """Pinned like tests/test_worker_brief.py pins phrases: this sentence is the whole mechanism"""
    prompt = _prompt(store, spec, _child(store, [_dep(store)["id"]]))

    assert ("Your brief's claims about what a dependency shipped are a HINT; this block "
            "is the record." in prompt)


def test_the_block_sits_before_the_operating_contract(store, spec):
    prompt = _prompt(store, spec, _child(store, [_dep(store)["id"]]))
    assert prompt.index(HEADING) < prompt.index("# Operating contract")


def test_no_edges_means_no_block_at_all(store, spec):
    prompt = _prompt(store, spec, store.create_work_order("standalone", "d"))
    assert HEADING not in prompt


def test_a_deleted_dependency_renders_missing_and_never_raises(store, spec):
    dep = _dep(store)
    child = _child(store, [dep["id"]])
    store.delete_work_order(dep["id"])

    prompt = _prompt(store, spec, child)
    assert f"- `{dep['id']}` [missing] — this dependency's record is gone" in prompt


def test_an_empty_result_summary_reads_as_none_recorded_not_none(store, spec):
    dep = _dep(store, summary="", pr="")
    prompt = _prompt(store, spec, _child(store, [dep["id"]]))

    assert "result: (none recorded)" in prompt
    assert "PR: (none)" in prompt
    assert "None" not in prompt.split(HEADING, 1)[1].splitlines()[2]


def test_edge_order_is_preserved(store, spec):
    first = _dep(store, title="one", summary="s1", pr="p1")
    second = _dep(store, title="two", summary="s2", pr="p2")
    prompt = _prompt(store, spec, _child(store, [first["id"], second["id"]]))

    assert prompt.index(first["id"]) < prompt.index(second["id"])


# -- dependency_context

@pytest.mark.parametrize("kind", ["manager", "planner", "analyst", "investigator"])
def test_only_a_worker_gets_the_record(store, spec, kind):
    """A manager writes no code and an analyst never builds on a dependency"""
    dep = _dep(store)
    wo = store.create_work_order("not a worker", "d", depends_on=[dep["id"]], kind=kind)
    assert dispatch.dependency_context(store, wo) == []


def test_a_row_with_no_kind_is_treated_as_a_worker(store, spec):
    dep = _dep(store)
    wo = {**_child(store, [dep["id"]]), "kind": None}
    assert [d["id"] for d in dispatch.dependency_context(store, wo)] == [dep["id"]]


def test_the_context_carries_only_the_five_recorded_fields(store, spec):
    dep = _dep(store)
    [ctx] = dispatch.dependency_context(store, _child(store, [dep["id"]]))
    assert set(ctx) == {"id", "status", "title", "result_summary", "pr_url"}


def test_a_missing_row_carries_nothing_but_id_and_status(store, spec):
    dep = _dep(store)
    child = _child(store, [dep["id"]])
    store.delete_work_order(dep["id"])
    assert dispatch.dependency_context(store, child) == [
        {"id": dep["id"], "status": "missing"}]


# -- determinism (the property tests/test_context.py:458 relies on)

def test_the_prompt_stays_deterministic(store, spec):
    child = _child(store, [_dep(store)["id"]])
    assert _prompt(store, spec, child) == _prompt(store, spec, child)
