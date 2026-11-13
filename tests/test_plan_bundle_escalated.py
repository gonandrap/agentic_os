"""Issue 1716: a bundled assumption whose plan review Neo escalated is the user's, not Neo's"""
import json
import os
import subprocess

import pytest

from jarvis import autoreview, ops
from jarvis.daemon import reviews_assumptions
from jarvis.neo_store import NeoStore
from jarvis.project_store import ProjectStore

OWN = "docs/superpowers/specs/onboarding.md"
CHOICE = "from section 4 of design doc onboarding.md: spawn_background + attach"


@pytest.fixture()
def store(project):
    s = ProjectStore(project)
    yield s
    s.close()


@pytest.fixture()
def auto_review(jarvis_home, fake_claude, catalog_file, project, monkeypatch):
    ops.start_os(str(catalog_file), foreground=True)
    monkeypatch.setattr(ops, "auto_review_at", lambda _path: True)


def _planner_with_bundled_row(store, project):
    fo = store.create_feature_order("Onboard via UI", description="onboard via the UI")
    store.update_feature_order(fo["id"], status="plan_review")
    planner = store.create_work_order("plan it", kind="planner", parent_id=fo["id"])
    store.update_feature_order(fo["id"], plan_wo_id=planner["id"])
    # The planner's own spec, committed after the feature order: same-family, so bundled
    (project / OWN).parent.mkdir(parents=True, exist_ok=True)
    (project / OWN).write_text("# Onboarding\n\n## 4. Help\n\nspawn_background + attach.\n")
    stamp = f"@{int(float(fo['created_at'])) + 3600} +0000"
    env = {**os.environ, "GIT_COMMITTER_DATE": stamp, "GIT_AUTHOR_DATE": stamp}
    for args in (("add", "--", OWN), ("commit", "-qm", "spec")):
        subprocess.run(["git", "-C", str(project), "-c", "user.email=t@t", "-c",
                        "user.name=t", *args], check=True, env=env)
    store.add_assumption(planner["id"], CHOICE, spec_path=OWN, spec_which="4",
                         spec_text="### § 4\nspawn_background + attach.")
    row = store.all_assumptions(planner["id"])[-1]
    neo = NeoStore()
    try:
        q = neo.ask("proj_a", planner["id"], "review this plan", kind="plan")
    finally:
        neo.close()
    store.update_feature_order(fo["id"], plan_question_id=q["id"], plan=json.dumps(
        {"bundled_assumptions": [{"id": row["id"], "n": 1, "content": CHOICE}]}))
    store.set_status(planner["id"], "needs_review")
    return fo["id"], q["id"], store.get_work_order(planner["id"])


def test_an_escalated_plan_leaves_its_bundled_rows_with_the_user(auto_review, store, project):
    fo_id, qid, wo = _planner_with_bundled_row(store, project)

    # Neo still holds the plan: the row is Neo's, and says it rides the plan
    assert reviews_assumptions(store, wo)
    line = ops.assumption_routing_line(ops.assumptions_with_rulings(store, wo["id"])[0])
    assert line == (f"Bundled into plan review Q{qid} of {fo_id}; "
                    f"Neo rules on it with the plan")

    neo = NeoStore()
    try:
        neo.mark(qid, "escalated", "13 children > 10 cap", cause="scope-too-large")
    finally:
        neo.close()

    assert autoreview.plan_bundle(store, wo)["with_user"]
    assert not reviews_assumptions(store, wo)
    # Still owed to the user, so the flag and ping fire, naming the plan decision
    from jarvis import invariants
    assert [a["id"] for a in invariants.assumptions_owed_to_user(store, wo)] == [
        a["id"] for a in store.pending_assumptions(wo["id"])]
    ops.flag_assumptions_pending(store, wo["id"])
    note = store.unrouted_notifications()[-1]
    assert f"jarvis fo approve {fo_id}" in note["body"]
    assert "jarvis wo review" not in note["body"]
    line = ops.assumption_routing_line(ops.assumptions_with_rulings(store, wo["id"])[0])
    assert line == (f"Bundled into plan review Q{qid}, escalated to you "
                    f"(scope-too-large); `jarvis fo approve {fo_id}` settles it")
    why = ops.waiting_on(store, wo)
    assert why["what"] == "plan_escalated"
    assert f"`jarvis fo approve {fo_id}` settles it" in why["detail"]


@pytest.mark.parametrize("closer, field", [("neo_refined", "neo_question_id"),
                                           ("question_lapsed", "question_id")])
def test_a_refined_or_lapsed_question_ends_its_hold(store, closer, field):
    from jarvis import holds

    wo = store.create_work_order("ask it")
    store.add_event(wo["id"], "question_asked", {"neo_question_id": 4426})
    store.add_event(wo["id"], closer, {field: 4426})
    assert not [h for h in holds.held(store, wo["id"]) if h.open]
