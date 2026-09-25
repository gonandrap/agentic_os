"""The analyst: the one work order an improvement order dispatches.

Section 3 of docs/superpowers/specs/2026-09-23-improvement-orders.md. Two halves, and
they fail in different ways:

* **The daemon files it.** A sibling loop beside the planner loop, idempotent by status,
  and the paired control here is a FEATURE order in the same tick — the two loops read
  the same table and a kind filter dropped on either side is invisible from one of them.
* **The briefing is not the worker contract.** An analyst that silently inherits the
  worker prompt is told to open a pull request, which is the one thing it must never do.
"""

from __future__ import annotations

import pytest

from jarvis import ops
from jarvis.catalog import ProjectSpec, load_catalog
from jarvis.daemon import Daemon
from jarvis.project_store import ProjectStore


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


@pytest.fixture()
def store(project):
    s = ProjectStore(project)
    yield s
    s.close()


def analysts(store) -> list[dict]:
    return [w for w in store.list_work_orders(limit=200) if w.get("kind") == "analyst"]


# -- the daemon loop --------------------------------------------------------------------


def test_a_pending_improvement_order_gets_exactly_one_analyst(started, store,
                                                              improvement_order):
    daemon = started
    daemon.plan_features(daemon.catalog.projects[0], store)

    opened = analysts(store)
    assert len(opened) == 1, opened
    wo = opened[0]
    assert wo["parent_id"] == improvement_order["id"]
    # The observation VERBATIM: the record has to read the same in `jarvis wo show` as
    # it does in `jarvis io show` (spec §3.1).
    assert wo["description"] == improvement_order["description"]

    io = store.get_feature_order(improvement_order["id"])
    assert io["status"] == "planning"
    assert io["plan_wo_id"] == wo["id"]


def test_a_second_tick_files_no_second_analyst(started, store, improvement_order):
    daemon = started
    project = daemon.catalog.projects[0]
    daemon.plan_features(project, store)
    daemon.plan_features(project, store)

    assert len(analysts(store)) == 1


def test_a_feature_order_in_the_same_tick_still_gets_a_planner_and_no_analyst(
        started, store, improvement_order):
    daemon = started
    ops.create_feature_order("proj_a", "CSV export", description=(
        "Add a CSV exporter to the reporting module, with a command that calls it and "
        "tests over the happy path and an empty result set."))
    daemon.plan_features(daemon.catalog.projects[0], store)

    kinds = sorted(w["kind"] for w in store.list_work_orders(limit=200))
    assert kinds == ["analyst", "planner"], kinds
    planner = [w for w in store.list_work_orders(limit=200)
               if w["kind"] == "planner"][0]
    assert planner["parent_id"].startswith("fo-")
    assert analysts(store)[0]["parent_id"].startswith("io-")


# -- the briefing -----------------------------------------------------------------------


@pytest.fixture()
def analyst_prompt(project, store, improvement_order):
    from jarvis.dispatch import build_worker_prompt, feature_context

    wo = store.create_work_order(title="Analyse: x", description="the observation",
                                 origin="jarvis", kind="analyst",
                                 parent_id=improvement_order["id"])
    spec = ProjectSpec(name="proj_a", path=project, description="")
    return build_worker_prompt(wo, spec, None,
                               feature=feature_context(store, wo))


def test_the_analyst_is_told_to_produce_a_diagnosis_and_not_a_fix(analyst_prompt):
    assert "You do not fix anything" in analyst_prompt
    assert "You produce a diagnosis" in analyst_prompt
    assert "has FAILED even if the fix is good" in analyst_prompt


def test_the_analyst_must_argue_against_the_cheapest_fix(analyst_prompt):
    """The corollary the whole feature exists for (spec §3.2.1)."""
    assert "cheapest fix" in analyst_prompt
    assert "why it is insufficient" in analyst_prompt


def test_the_terminal_action_is_the_report_and_never_wo_finish(analyst_prompt,
                                                               improvement_order):
    assert f"jarvis io report {improvement_order['id']} --from-file" in analyst_prompt
    # An analyst that runs `wo finish` settles itself before the report is stored.
    assert "jarvis wo finish" not in analyst_prompt.replace(
        "Do not run `jarvis wo finish`", "")


def test_the_analyst_is_never_told_to_open_a_pull_request(analyst_prompt):
    """The worker contract's PR rule reaching an analyst is the failure mode §3.1 names."""
    assert "open NO pull request" in analyst_prompt
    assert "with a pull request" not in analyst_prompt


def test_the_report_skeleton_matches_the_spec_field_names(analyst_prompt):
    for field in ("summary", "findings", "key", "symptom", "root_cause", "evidence",
                  "why_insufficient", "recommendation", "proposed_orders",
                  "justification"):
        assert f'"{field}"' in analyst_prompt, field


def test_every_stored_evidence_ref_is_rendered_with_the_command_that_reads_it(
        analyst_prompt):
    assert "jarvis wo show wo-11111111" in analyst_prompt
    assert "gh issue view 42" in analyst_prompt
    assert "https://example.invalid/x" in analyst_prompt


def test_at_most_six_findings(analyst_prompt):
    assert "6 findings" in analyst_prompt


def test_every_ref_shape_in_the_table_gets_its_command():
    """The §3.3 mapping is the deliverable, and the fixture reaches only three of its
    rows. Each shape is checked directly, including the unrecognised one — which is
    LISTED rather than dropped: a ref the user attached and the OS cannot name is the
    analyst's problem to solve, not the OS's to hide."""
    from jarvis.dispatch import _evidence_checklist

    lines = _evidence_checklist([
        "wo-11111111", "fo-22222222", "io-33333333", "al-44444444", "#42",
        "https://github.invalid/o/r/pull/7", "https://example.invalid/x", "nonsense-ref",
    ])
    joined = "\n".join(lines)

    assert len(lines) == 8, joined
    assert "jarvis wo show wo-11111111" in joined
    assert "jarvis fo show fo-22222222" in joined
    assert "jarvis io show io-33333333" in joined
    assert "jarvis alarms" in joined
    assert "gh issue view 42" in joined
    assert "gh pr view https://github.invalid/o/r/pull/7" in joined
    assert "https://example.invalid/x — fetch it" in joined
    assert "nonsense-ref" in joined and "work out how to read it" in joined


# -- controls: the other three kinds are untouched --------------------------------------


def test_a_worker_still_gets_the_worker_contract(project):
    from jarvis.dispatch import build_worker_prompt

    spec = ProjectSpec(name="proj_a", path=project, description="")
    prompt = build_worker_prompt({"id": "wo-1", "title": "t", "description": "d",
                                  "kind": "worker"}, spec, None)
    assert "[wo-1]" in prompt


def test_a_planner_still_names_its_seats_and_the_analyst_names_neither(project,
                                                                      analyst_prompt):
    from jarvis.dispatch import build_worker_prompt

    spec = ProjectSpec(name="proj_a", path=project, description="")
    planner = build_worker_prompt({"id": "wo-1", "title": "t", "description": "d",
                                   "kind": "planner", "parent_id": "fo-1"}, spec, None)
    for seat in ("jarvis-architect", "jarvis-test-lead"):
        assert seat in planner
        assert seat not in analyst_prompt


def test_the_analyst_never_gets_the_workers_pr_title_rule(analyst_prompt):
    """The worker contract's `[wo-…] ` title requirement reaching an analyst would be an
    instruction to open the one thing it must never open."""
    assert "[wo-" not in analyst_prompt


def test_a_manager_is_unaffected_by_the_feature_context_change(project, store):
    """`feature_context` grew a second kind; a manager must still receive its REAL
    children, not the analyst's empty list."""
    from jarvis.dispatch import build_worker_prompt, feature_context

    fo = store.create_feature_order("CSV export", description="an exporter")
    manager = store.create_work_order(title="Manage", description="d", origin="jarvis",
                                      kind="manager", parent_id=fo["id"])
    store.create_work_order(title="Build it", description="d", origin="jarvis",
                            kind="worker", parent_id=fo["id"])

    ctx = feature_context(store, manager)
    assert [c["title"] for c in ctx["children"]] == ["Build it"]

    spec = ProjectSpec(name="proj_a", path=project, description="")
    prompt = build_worker_prompt(manager, spec, None, feature=ctx)
    assert "You are the PROJECT MANAGER" in prompt


def test_a_running_analyst_spends_a_project_slot(store, improvement_order):
    """§3.1: `count_active` has NO kind filter and that is CORRECT here — an analyst is
    short-lived like a planner, so it should spend a slot while it runs. Verified rather
    than assumed (kn-52e51faf); the manager exemption is the only one."""
    before = store.count_active()
    wo = store.create_work_order(title="Analyse: x", description="d", origin="jarvis",
                                 kind="analyst", parent_id=improvement_order["id"])
    store.update_work_order(wo["id"], status="running")

    assert store.count_active() == before + 1


def test_an_analyst_gets_no_planning_seats(project):
    from jarvis.bootstrap import install_agent_assets

    analyst = install_agent_assets(project, kind="analyst")
    planner = install_agent_assets(project, kind="planner")
    assert not any(p.name == "agent-seats" for p in analyst), analyst
    assert any(p.name == "agent-seats" for p in planner), planner


# -- delivery ---------------------------------------------------------------------------


def test_the_dispatched_analyst_turn_carries_the_analyst_prompt(started, fake_claude,
                                                                improvement_order):
    daemon = started
    daemon.tick()

    prompt = fake_claude.wait_calls(
        lambda c: "--session-id" in c["argv"], count=1)[0]["argv"][-1]
    assert "You are an ANALYST" in prompt
    assert f"jarvis io report {improvement_order['id']} --from-file" in prompt


def test_the_supervisor_names_an_analyst(improvement_order):
    from jarvis.supervisor import _what_it_is

    what = _what_it_is({"id": "wo-1", "kind": "analyst",
                        "parent_id": improvement_order["id"]})
    assert "ANALYST" in what
    assert "an ordinary work order" not in what
    assert improvement_order["id"] in what
