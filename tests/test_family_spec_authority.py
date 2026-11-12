"""A spec written inside a feature order is not authority for it (docs/superpowers/specs/2026-11-12-a-family-spec-is-not-authority.md)"""

from __future__ import annotations

import json
import os
import subprocess

import pytest

from jarvis import autoreview, ops, plans, specs
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.project_store import ProjectStore
from jarvis.testing import FIXTURE_DESIGN_DOC

ASK = ("Let me onboard a project from the UI; at any time I can ask for help and a new "
       "claude session is open for that context.")
OWN = "docs/superpowers/specs/onboarding.md"
CHOICE = f"Help is a background session the user attaches to, per {OWN} § 4"


@pytest.fixture()
def store(project):
    s = ProjectStore(project)
    yield s
    s.close()


def write_own_spec(project, created_at: float) -> None:
    """The planner's spec, committed AFTER its feature order was filed"""
    (project / OWN).write_text("# Onboarding\n\n## 4. Help\n\nspawn_background + attach.\n")
    stamp = f"@{int(created_at) + 3600} +0000"
    env = {**os.environ, "GIT_COMMITTER_DATE": stamp, "GIT_AUTHOR_DATE": stamp}
    for args in (("add", "--", OWN), ("commit", "-qm", "spec")):
        subprocess.run(["git", "-C", str(project), "-c", "user.email=t@t", "-c",
                        "user.name=t", *args], check=True, env=env)


def family(store, project, *, status: str = "planning"):
    fo = store.create_feature_order("Onboard via UI", description=ASK)
    store.update_feature_order(fo["id"], status=status)
    planner = store.create_work_order("plan it", kind="planner", parent_id=fo["id"])
    store.update_feature_order(fo["id"], plan_wo_id=planner["id"])
    write_own_spec(project, float(fo["created_at"]))
    return store.get_feature_order(fo["id"]), planner


def cite(store, wo_id: str, path: str) -> dict:
    store.add_assumption(wo_id, CHOICE, spec_path=path, spec_which="4",
                         spec_text="### § 4\nspawn_background + attach.")
    return store.all_assumptions(wo_id)[-1]


def test_a_spec_the_feature_wrote_is_its_own_claim_and_a_prior_one_is_not(store, project):
    fo, planner = family(store, project)

    fo_id, own = specs.family_written(store, planner, [OWN, FIXTURE_DESIGN_DOC])

    assert fo_id == fo["id"]
    assert own == {OWN}


def test_the_review_packet_says_not_authority_and_carries_the_users_words(store,
                                                                          project):
    fo, planner = family(store, project)
    a = cite(store, planner["id"], OWN)

    text = autoreview._ruling_question(
        "proj_a", planner, a, [a],
        family=autoreview.family_context(store, planner, a, None))

    assert autoreview.FAMILY_HEADING.format(fo=fo["id"]) in text
    assert autoreview.CITED_HEADING not in text
    assert ASK in text
    assert "ambiguous-intent" in text


def test_control_a_pre_existing_spec_is_still_authority(store, project):
    _, planner = family(store, project)
    a = cite(store, planner["id"], FIXTURE_DESIGN_DOC)

    assert autoreview.family_context(store, planner, a, None) is None
    assert autoreview.bundled_into_plan(store, planner, a) == ""
    text = autoreview._ruling_question("proj_a", planner, a, [a], family=None)
    assert autoreview.CITED_HEADING in text


def test_only_a_planner_row_rides_an_open_plan_review(store, project):
    fo, planner = family(store, project)
    kid = store.create_work_order("build it", parent_id=fo["id"])

    assert autoreview.bundled_into_plan(store, planner, cite(store, planner["id"], OWN)) \
        == fo["id"]
    assert autoreview.bundled_into_plan(store, kid, cite(store, kid["id"], OWN)) == ""
    store.update_feature_order(fo["id"], status="executing")
    assert autoreview.bundled_into_plan(store, planner,
                                        store.all_assumptions(planner["id"])[0]) == ""


def test_the_plan_question_lists_each_bundled_choice_under_the_ask():
    plan = {"children": [], "bundled_assumptions": [{"id": 1, "n": 2, "content": CHOICE}]}

    text = plans.build_plan_question({"id": "fo-1", "title": "t", "description": ASK},
                                     plan)

    assert text.index(ASK) < text.index(plans.BUNDLED_HEADING) < text.index(
        f"#2: {CHOICE}")


# -- end to end: submit carries them, release settles them

@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


def test_a_released_plan_settles_the_rows_it_carried(started, store, project):
    daemon = started
    fo = ops.create_feature_order("proj_a", "Onboard via UI", description=ASK)
    daemon.tick()
    fo = store.get_feature_order(fo["id"])
    write_own_spec(project, float(fo["created_at"]))
    carried = cite(store, fo["plan_wo_id"], OWN)
    unrelated = cite(store, fo["plan_wo_id"], FIXTURE_DESIGN_DOC)

    ops.submit_plan(fo["id"], {"summary": "onboarding FORCE_APPROVE",
                               "design_doc": FIXTURE_DESIGN_DOC,
                               "children": [{
                                   "key": "ui", "title": "Build ui", "needs": [],
                                   "spec_section": "1",
                                   "description": (
                                       "Build the onboarding page: add the module, wire "
                                       "it into the command that calls it, and cover "
                                       "both paths with tests in the existing suite. "
                                       "FORCE_APPROVE")}]})
    asked = store.get_feature_order(fo["id"])
    daemon._neo_drain()

    assert store.get_feature_order(fo["id"])["status"] == "executing"
    rows = {a["id"]: a for a in store.all_assumptions(fo["plan_wo_id"])}
    assert rows[carried["id"]]["status"] == "accepted"
    assert rows[carried["id"]]["decided_by"] == "neo"
    assert rows[unrelated["id"]]["status"] != "accepted"
    assert [b["id"] for b in json.loads(asked["plan"])["bundled_assumptions"]] \
        == [carried["id"]]
