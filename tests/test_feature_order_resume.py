"""Getting a feature order out of `failed`.

`Daemon.settle_features` only ever looks at `executing` features, which makes `failed`
terminal: a child that recovers afterwards leaves the feature failed for ever. fo-e353491c
sat like that showing `12/12 done` and a reason naming a `completed` work order, and the
only way out was an edit to the production database.

Two ways back, and the tests below are mostly about the seam between them: the automatic
`INV-FEATURE-FALSE-FAILURE`, and `jarvis fo resume` for a child that is genuinely still
dead. Design: docs/superpowers/specs/2026-08-29-feature-order-resume.md.
"""

from __future__ import annotations

import pytest

from jarvis import invariants, ops
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.project_store import SUPERSEDED_CHILDREN_KEY, ProjectStore
from jarvis.testing import FIXTURE_DESIGN_DOC

ASK = ("Add a CSV exporter to the reporting module, with a command that calls it and "
       "tests over both the happy path and an empty result set.")


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


@pytest.fixture()
def store(project):
    s = ProjectStore(project)
    yield s
    s.close()


def child(key: str) -> dict:
    return {
        "key": key,
        "title": f"Build {key}",
        "description": (
            f"Build the {key} part of the exporter: add the module, wire it into the "
            f"command that calls it, and cover both paths with tests in the existing "
            f"suite. Do not change the public interface of the caller. FORCE_APPROVE"
        ),
        "needs": [],
    }


def a_released_feature(daemon, store, *keys: str) -> dict:
    fo = ops.create_feature_order("proj_a", "CSV export", description=ASK)
    daemon.tick()
    ops.submit_plan(fo["id"], {"summary": "an exporter FORCE_APPROVE",
                               "design_doc": FIXTURE_DESIGN_DOC,
                               "children": [child(k) for k in keys]})
    daemon._neo_drain()
    daemon.tick()
    return store.get_feature_order(fo["id"])


def a_failed_feature(daemon, store, *keys: str) -> tuple[dict, list[dict]]:
    """A feature settled `failed` by its first child, the rest completed."""
    fo = a_released_feature(daemon, store, *keys)
    kids = store.feature_children(fo["id"])
    store.set_status(kids[0]["id"], "failed")
    for c in kids[1:]:
        store.set_status(c["id"], "completed")
    daemon.tick()
    assert store.get_feature_order(fo["id"])["status"] == "failed"
    return store.get_feature_order(fo["id"]), kids


# -- INV-FEATURE-FALSE-FAILURE: the child recovered on its own ---------------------------


def test_a_feature_goes_back_to_work_once_its_dead_child_recovers(started, store):
    """The whole bug in one test. `settle_features` never looks at a settled feature, so
    without the invariant the second half of this never happens however many ticks run."""
    fo, kids = a_failed_feature(started, store, "one", "two")
    store.set_status(kids[0]["id"], "completed")  # `jarvis wo done`, a retry, a late merge

    found = invariants.check_project(store)

    assert [v.invariant for v in found if v.invariant == "INV-FEATURE-FALSE-FAILURE"]
    reopened = store.get_feature_order(fo["id"])
    assert reopened["status"] == "executing"
    assert reopened["needs_attention"] == 0


def test_the_reopened_feature_is_then_settled_by_the_ordinary_path(started, store):
    """The invariant decides NOTHING — it hands the feature back to `settle_features`,
    which reaches the same answer it would have reached the first time. A repair that
    completed the feature itself would be a second place that ends a feature, and the two
    would drift the day validation is switched on."""
    fo, kids = a_failed_feature(started, store, "one", "two")
    store.set_status(kids[0]["id"], "completed")

    invariants.check_project(store)
    started.tick()

    settled = store.get_feature_order(fo["id"])
    assert settled["status"] == "completed"
    assert settled["needs_attention"] == 0


def test_a_feature_with_a_still_dead_child_is_left_alone(started, store):
    """The invariant repairs only what is unambiguous. A child that is still failed is
    still a real failure, and reopening on it would drop the flag the user needs."""
    fo, _kids = a_failed_feature(started, store, "one", "two")

    found = invariants.check_project(store)

    assert not [v for v in found if v.invariant == "INV-FEATURE-FALSE-FAILURE"]
    still = store.get_feature_order(fo["id"])
    assert still["status"] == "failed" and still["needs_attention"] == 1


def test_the_settler_and_the_invariant_cannot_flap(started, store):
    """Both sides read `dead_feature_children`, so a feature the settler failed is one
    the invariant refuses to reopen. If they ever disagreed the feature would oscillate
    on every reconcile, and the user would watch the flag blink. Ticked repeatedly,
    because one pass proves nothing about a loop."""
    fo, _kids = a_failed_feature(started, store, "one", "two")

    for _ in range(8):
        started.tick()
        invariants.check_project(store)

    assert store.get_feature_order(fo["id"])["status"] == "failed"


def test_doctor_reports_the_false_failure_without_curing_it(started, store):
    """`jarvis doctor` with no `--repair` is a pure read. Reporting that a feature is
    wrongly failed must not be the thing that un-fails it — `set_feature_status` and
    `clear_feature_attention` are on `_ReadOnly._BLOCKED` for exactly this."""
    fo, kids = a_failed_feature(started, store, "one", "two")
    store.set_status(kids[0]["id"], "completed")

    found = invariants.check_project(store, repair=False)

    assert [v for v in found if v.invariant == "INV-FEATURE-FALSE-FAILURE"]
    assert store.get_feature_order(fo["id"])["status"] == "failed"


def test_a_feature_with_no_children_at_all_is_not_reopened(started, store):
    """A feature failed before its plan was ever released has nothing to re-derive from,
    and `all([])` would otherwise complete it — announcing a delivery of nothing."""
    fo = ops.create_feature_order("proj_a", "CSV export", description=ASK)
    store.set_feature_status(fo["id"], "failed")
    store.flag_feature_attention(fo["id"], "the planner died")

    invariants.check_project(store)

    assert store.get_feature_order(fo["id"])["status"] == "failed"


# -- `jarvis fo resume`: the user answering for a child that is still dead ---------------


def test_resume_supersedes_the_dead_child_and_files_the_fix(started, store):
    fo, kids = a_failed_feature(started, store, "one", "two")

    out = ops.resume_feature_order(fo["id"], fix="redo the exporter, without the shim")

    assert out["superseded"] == [kids[0]["id"]]
    assert store.get_feature_order(fo["id"])["status"] == "executing"
    filed = store.get_work_order(out["fix_wo_id"])
    assert filed["parent_id"] == fo["id"]
    # The description is the ONLY thing that worker will see, so it is the user's words
    # verbatim; the title is a derived label and may be truncated.
    assert filed["description"] == "redo the exporter, without the shim"


def test_a_superseded_child_stops_failing_its_feature(started, store):
    """The point of the whole mechanism. The dead child is still `failed` in the tree —
    nothing lies about it — and the feature completes anyway once the corrective work
    lands."""
    fo, kids = a_failed_feature(started, store, "one", "two")
    out = ops.resume_feature_order(fo["id"], fix="redo the exporter, without the shim")

    store.set_status(out["fix_wo_id"], "completed")
    started.tick()

    assert store.get_feature_order(fo["id"])["status"] == "completed"
    assert store.get_work_order(kids[0]["id"])["status"] == "failed"


def test_a_superseded_child_also_stops_holding_completion_up(started, store):
    """The other half, and the one that is easy to miss: a child excluded from the dead
    check but still counted towards `all completed` would leave the feature `executing`
    for ever, which is a worse failure than the one being fixed — it raises nothing."""
    fo, kids = a_failed_feature(started, store, "one", "two")

    ops.resume_feature_order(fo["id"])  # no fix: the work is already done
    started.tick()

    assert store.get_feature_order(fo["id"])["status"] == "completed"


def test_resume_without_a_fix_files_nothing(started, store):
    fo, _kids = a_failed_feature(started, store, "one", "two")

    out = ops.resume_feature_order(fo["id"])

    assert out["fix_wo_id"] is None
    assert len(store.feature_children(fo["id"])) == 2


def test_resume_refuses_a_feature_that_is_not_failed(started, store):
    """`cancelled` was the user's own decision and `completed` has nothing to resume.
    Reversing a cancellation is a different act with different consequences for the
    children they stopped, so it is not smuggled in under this command."""
    fo = a_released_feature(started, store, "one")
    ops.cancel_feature_order(fo["id"])

    with pytest.raises(ops.OpsError, match="not failed"):
        ops.resume_feature_order(fo["id"])


def test_resuming_twice_records_the_first_decision_only(started, store):
    """Idempotent on the child id, and the FIRST note is kept — it is the one that was
    true when the decision was taken."""
    fo, kids = a_failed_feature(started, store, "one", "two")
    ops.resume_feature_order(fo["id"], fix="first answer")
    store.set_feature_status(fo["id"], "failed")  # as if it failed again

    ops.resume_feature_order(fo["id"], fix="second answer")

    record = store.superseded_children(fo["id"])
    assert [s["wo_id"] for s in record] == [kids[0]["id"]]
    assert record[0]["note"] == "first answer"


def test_the_record_lives_where_a_feature_with_no_manager_can_hold_it(started, store):
    """`ops.feature_event` — the obvious home — returns False when a feature has no
    project manager order, which is every feature planned while `os.validation.enabled`
    was false, including the one that motivated this. A record that silently fails to be
    written for its own motivating case is not a record."""
    fo, kids = a_failed_feature(started, store, "one", "two")
    assert store.manager_work_order(fo["id"]) is None

    ops.resume_feature_order(fo["id"], fix="redo it")

    assert not ops.feature_events_of_kind(store, fo["id"], "child_superseded")
    assert [s["wo_id"] for s in store.superseded_children(fo["id"])] == [kids[0]["id"]]


# -- the surfaces ------------------------------------------------------------------------


def test_a_superseded_child_stays_in_the_tree_marked(started, store):
    """Superseded, never hidden. Billing and cancellation still want the row, and a child
    the user answered for that VANISHED would look like one that never ran."""
    fo, kids = a_failed_feature(started, store, "one", "two")
    ops.resume_feature_order(fo["id"], fix="redo it")

    detail = ops.show_feature_order(fo["id"])

    by_id = {c["id"]: c for c in detail["children"]}
    assert by_id[kids[0]["id"]]["superseded"] is True
    assert by_id[kids[1]["id"]]["superseded"] is False


def test_the_feature_page_offers_the_form_and_marks_the_child(started, store):
    from fastapi.testclient import TestClient

    from jarvis.ui.app import create_app

    ui_client = TestClient(create_app(), follow_redirects=False)
    fo, kids = a_failed_feature(started, store, "one", "two")

    page = ui_client.get(f"/fo/proj_a/{fo['id']}").text
    assert f"/fo/proj_a/{fo['id']}/resume" in page
    assert 'name="fix"' in page

    ui_client.post(f"/fo/proj_a/{fo['id']}/resume", data={"fix": "redo it"},
                   follow_redirects=False)

    assert store.get_feature_order(fo["id"])["status"] == "executing"
    after = ui_client.get(f"/fo/proj_a/{fo['id']}").text
    assert "superseded" in after
    assert kids[0]["id"] in after  # still listed, not dropped
    # Gone from the page the moment there is nothing to resume.
    assert f"/fo/proj_a/{fo['id']}/resume" not in after


def test_the_cli_resumes_and_says_what_it_filed(started, store, capsys):
    from jarvis import cli

    fo, _kids = a_failed_feature(started, store, "one", "two")

    cli.main(["fo", "resume", fo["id"], "--fix", "redo the exporter"])

    out = capsys.readouterr().out
    assert "executing" in out
    assert store.get_feature_order(fo["id"])["status"] == "executing"
    assert any(c["title"] == "redo the exporter"
               for c in store.feature_children(fo["id"]))


def test_the_metadata_key_survives_a_feature_that_already_had_metadata(started, store):
    """`metadata` is one JSON blob shared with whatever else ever wants a key there, so
    the write must merge rather than replace."""
    fo, kids = a_failed_feature(started, store, "one", "two")
    store.update_feature_order(fo["id"], metadata='{"something_else": 1}')

    ops.resume_feature_order(fo["id"])

    import json
    meta = json.loads(store.get_feature_order(fo["id"])["metadata"])
    assert meta["something_else"] == 1
    assert [s["wo_id"] for s in meta[SUPERSEDED_CHILDREN_KEY]] == [kids[0]["id"]]
