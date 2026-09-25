"""Improvement orders: a `feature_orders` row with `kind='improvement'`.

Section 2 of docs/superpowers/specs/2026-09-23-improvement-orders.md — persistence and
the CLI surface. Two properties this file exists to protect:

* **A new kind must not leak into the feature-order listings.** Nothing read
  `feature_orders.kind` before this, so every unfiltered query picked the new rows up
  silently. Each one is pinned here with a positive `kind` filter, never a negative one.
* **…and must still reach the attention path.** An improvement order needing the user is
  exactly as much an attention item as a feature order is, so `flagged_feature_orders`
  stays unfiltered (§2.4).
"""

from __future__ import annotations

import inspect
import json

import pytest

from jarvis import cli, ops, project_store
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.project_store import ProjectStore

REFS = ("wo-11111111", "#42", "https://example.invalid/x")
OBSERVATION = ("Three work orders in a row spent their first turn re-reading the same "
               "module because nothing told them where the dispatch path lives.")


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


@pytest.fixture()
def store(project):
    s = ProjectStore(project)
    yield s
    s.close()


# -- the store: the kind column, and the queries it has to reach -----------------------

def test_the_kind_column_carries_both_values_and_defaults_to_feature(store):
    fo = store.create_feature_order("CSV export", description="the whole ask")
    io = store.create_feature_order("slow first turns", description=OBSERVATION,
                                    kind="improvement")
    assert fo["kind"] == "feature"
    assert io["kind"] == "improvement"
    assert io["id"].startswith("io-")
    assert fo["id"].startswith("fo-")


def test_an_unknown_kind_is_refused(store):
    with pytest.raises(AssertionError):
        store.create_feature_order("x", description="y", kind="investigation")


def test_an_improvement_order_is_absent_from_every_feature_listing(store):
    fo = store.create_feature_order("CSV export", description="the whole ask")
    io = store.create_feature_order("slow first turns", description=OBSERVATION,
                                    kind="improvement")

    assert [r["id"] for r in store.list_feature_orders()] == [fo["id"]]
    assert [r["id"] for r in store.list_feature_orders(kind="improvement")] == [io["id"]]
    assert sorted(r["id"] for r in store.list_feature_orders(kind=None)) == \
        sorted([fo["id"], io["id"]])

    assert store.feature_status_counts() == {"pending": 1}
    assert store.feature_status_counts(kind="improvement") == {"pending": 1}
    assert store.feature_status_counts(kind=None) == {"pending": 2}

    assert store.feature_summary()["by_status"] == {"pending": 1}
    assert store.feature_summary(kind="improvement")["by_status"] == {"pending": 1}


def test_the_attention_path_is_not_filtered(store):
    """§2.4: the filter goes on the listings, not on the attention path."""
    io = store.create_feature_order("slow first turns", description=OBSERVATION,
                                    kind="improvement")
    store.flag_feature_attention(io["id"], "findings awaiting you")
    assert [r["id"] for r in store.flagged_feature_orders()] == [io["id"]]


def test_the_planner_sweep_never_sees_an_improvement_order(store):
    """The exact call `Daemon.plan_features` makes, asserted here rather than on the
    daemon: a planner opened on an improvement order is the loudest failure in §2.4."""
    io = store.create_feature_order("slow first turns", description=OBSERVATION,
                                    kind="improvement")
    assert io["status"] == "pending"
    assert store.list_feature_orders(statuses=("pending",)) == []


def test_feature_order_for_planner_matches_an_analyst_row(store):
    """§2.4: `feature_order_for_planner` keys on `plan_wo_id` and will now match the
    analyst too. `INV-NEO-ESCALATIONS-LIVE` reads it, so prove it rather than reason."""
    io = store.create_feature_order("slow first turns", description=OBSERVATION,
                                    kind="improvement")
    wo = store.create_work_order("analyse it", description="…")
    store.update_feature_order(io["id"], plan_wo_id=wo["id"])
    assert store.feature_order_for_planner(wo["id"])["id"] == io["id"]
    assert store.feature_order_for_planner(io["id"])["id"] == io["id"]


def test_no_negative_kind_filter_exists_in_the_new_sql():
    """§2.4 again, from the other side: a negative filter excludes nothing that has not
    been invented yet, which is the bug this whole section is fixing."""
    for verb in (ProjectStore.list_feature_orders, ProjectStore.feature_status_counts,
                 ProjectStore.feature_summary, ProjectStore.flagged_feature_orders):
        src = inspect.getsource(verb)
        assert "kind !=" not in src and "kind<>" not in src and "kind NOT" not in src
    assert "kind=?" in inspect.getsource(ProjectStore.list_feature_orders)


def test_the_status_labels_are_kind_aware_and_leave_features_alone():
    label = project_store.feature_status_label
    assert label("improvement", "planning") == "analysing"
    assert label("improvement", "plan_review") == "findings awaiting you"
    assert label("improvement", "completed") == "completed"
    for status in project_store.FO_STATUSES:
        assert label("feature", status) == status


def test_is_feature_order_id():
    pred = project_store.is_feature_order_id
    assert pred("fo-abc") and pred("io-abc")
    assert not pred("wo-abc")
    assert not pred("al-abc")


# -- ops: filing one --------------------------------------------------------------------

def test_creating_an_improvement_order_stores_its_refs_verbatim(started, store):
    io = ops.create_improvement_order("proj_a", "slow first turns",
                                      description=OBSERVATION, refs=REFS)
    assert io["kind"] == "improvement"
    row = store.get_feature_order(io["id"])
    assert json.loads(row["metadata"])["evidence_refs"] == list(REFS)
    assert row["plan"] is None and row["plan_wo_id"] is None


def test_an_improvement_order_with_no_observation_is_refused(started):
    with pytest.raises(ops.OpsError) as e:
        ops.create_improvement_order("proj_a", "slow first turns", description="",
                                     refs=REFS)
    assert "jarvis io create proj_a" in str(e.value) and "-d" in str(e.value)


def test_an_improvement_order_on_an_unregistered_project_is_refused(started):
    with pytest.raises(ops.OpsError) as e:
        ops.create_improvement_order("nope", "t", description=OBSERVATION, refs=REFS)
    assert "not registered" in str(e.value) and "jarvis start" in str(e.value)


def test_an_improvement_order_with_no_evidence_is_refused(started):
    with pytest.raises(ops.OpsError) as e:
        ops.create_improvement_order("proj_a", "slow first turns",
                                     description=OBSERVATION, refs=())
    msg = str(e.value)
    assert "request for an opinion" in msg
    assert "--ref" in msg


# -- ops: the listings, the show, the guards --------------------------------------------

def test_the_two_listings_do_not_see_each_other(started):
    fo = ops.create_feature_order("proj_a", "CSV export", description="the whole ask")
    io = ops.create_improvement_order("proj_a", "slow first turns",
                                      description=OBSERVATION, refs=REFS)
    assert [r["id"] for r in ops.list_feature_orders()] == [fo["id"]]
    assert [r["id"] for r in ops.list_improvement_orders()] == [io["id"]]


def test_show_is_counts_first_and_survives_a_null_report(started):
    io = ops.create_improvement_order("proj_a", "slow first turns",
                                      description=OBSERVATION, refs=REFS)
    detail = ops.show_improvement_order(io["id"])
    assert detail["findings"] == 0
    assert detail["by_decision"] == {"accepted": 0, "rejected": 0, "pending": 0}
    assert detail["evidence_refs"] == list(REFS)
    assert detail["observation"] == OBSERVATION
    assert detail["analyst"] is None
    assert detail["status_label"] == "pending"
    assert "findings" not in detail.get("plan_text", "x")  # no per-finding renderer here


def test_show_counts_a_stored_report(started, store):
    io = ops.create_improvement_order("proj_a", "slow first turns",
                                      description=OBSERVATION, refs=REFS)
    store.update_feature_order(io["id"], plan=json.dumps({"findings": [
        {"key": "a", "status": "accepted"},
        {"key": "b", "status": "rejected"},
        {"key": "c"},
    ]}))
    detail = ops.show_improvement_order(io["id"])
    assert detail["findings"] == 3
    assert detail["by_decision"] == {"accepted": 1, "rejected": 1, "pending": 1}


def test_cancel_stops_the_analyst_and_settles_the_order(started, store):
    io = ops.create_improvement_order("proj_a", "slow first turns",
                                      description=OBSERVATION, refs=REFS)
    analyst = store.create_work_order("analyse it", description="…")
    store.update_feature_order(io["id"], plan_wo_id=analyst["id"], status="planning")
    res = ops.cancel_improvement_order(io["id"])
    assert res["status"] == "cancelled"
    assert analyst["id"] in res["cancelled_work_orders"]
    assert store.get_feature_order(io["id"])["status"] == "cancelled"


def test_the_feature_verbs_refuse_an_improvement_order(started, store):
    io = ops.create_improvement_order("proj_a", "slow first turns",
                                      description=OBSERVATION, refs=REFS)
    store.update_feature_order(io["id"], status="planning")
    with pytest.raises(ops.OpsError) as e:
        ops.submit_plan(io["id"], {"children": []})
    assert "jarvis io" in str(e.value)

    store.update_feature_order(io["id"], status="plan_review")
    with pytest.raises(ops.OpsError) as e:
        ops.review_plan(io["id"])
    assert "jarvis io" in str(e.value)

    store.update_feature_order(io["id"], status="failed")
    with pytest.raises(ops.OpsError) as e:
        ops.resume_feature_order(io["id"])
    assert "jarvis io" in str(e.value)


def test_the_io_verbs_refuse_a_feature_order(started):
    fo = ops.create_feature_order("proj_a", "CSV export", description="the whole ask")
    for call in (ops.show_improvement_order, ops.cancel_improvement_order):
        with pytest.raises(ops.OpsError) as e:
            call(fo["id"])
        assert "jarvis fo" in str(e.value)


def test_os_status_labels_an_improvement_order_by_kind(started, store):
    io = ops.create_improvement_order("proj_a", "slow first turns",
                                      description=OBSERVATION, refs=REFS)
    store.update_feature_order(io["id"], status="plan_review")
    store.flag_feature_attention(io["id"], "findings awaiting you")
    items = [i for i in ops.os_status()["attention"] if i.get("fo_id") == io["id"]]
    assert items and items[0]["status"] == "improvement:findings awaiting you"


# -- the budget passthrough --------------------------------------------------------------

def test_the_family_budget_works_on_an_io_id(started):
    io = ops.create_improvement_order("proj_a", "slow first turns",
                                      description=OBSERVATION, refs=REFS,
                                      budget_usd=4.0)
    assert ops.feature_order_budget(io["id"])["budget_usd"] == 4.0
    assert ops.set_feature_budget(io["id"], 9.0)["budget_usd"] == 9.0
    assert ops.feature_order_budget(io["id"])["budget_usd"] == 9.0
    assert ops.set_feature_budget(io["id"], None)["budget_usd"] is None


# -- the CLI ------------------------------------------------------------------------------

def test_the_cli_drives_the_whole_surface(started, store, capsys):
    assert cli.main(["io", "create", "proj_a", "slow first turns",
                     "-d", OBSERVATION, "--ref", "wo-11111111", "--ref", "#42",
                     "--budget", "5", "--json"]) == 0
    io_id = json.loads(capsys.readouterr().out)["created"]
    assert io_id.startswith("io-")

    assert cli.main(["io", "list", "--json"]) == 0
    assert [r["id"] for r in json.loads(capsys.readouterr().out)] == [io_id]

    assert cli.main(["io", "show", io_id, "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["evidence_refs"] == ["wo-11111111", "#42"]

    assert cli.main(["io", "budget", io_id, "7", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["budget_usd"] == 7.0
    assert cli.main(["io", "budget", io_id, "--clear", "--json"]) == 0

    assert cli.main(["io", "cancel", io_id, "--json"]) == 0
    capsys.readouterr()
    assert store.get_feature_order(io_id)["status"] == "cancelled"

    # …and it stays out of the feature-order listing throughout
    assert cli.main(["fo", "list", "--all", "--json"]) == 0
    assert json.loads(capsys.readouterr().out) == []


def test_create_promises_no_analyst_it_cannot_dispatch(started, capsys):
    assert cli.main(["io", "create", "proj_a", "slow first turns",
                     "-d", OBSERVATION, "--ref", "wo-11111111", "--json"]) == 0
    note = json.loads(capsys.readouterr().out)["note"]
    assert "analyst" not in note
    assert "nothing is filed" in note


def test_cost_accepts_an_io_id(started):
    io = ops.create_improvement_order("proj_a", "slow first turns",
                                      description=OBSERVATION, refs=REFS)
    assert cli.main(["cost", io["id"]]) == 0


# -- the shared fixture (§2.7) ------------------------------------------------------------

def test_the_shared_fixture_files_one(improvement_order, store):
    row = store.get_feature_order(improvement_order["id"])
    assert row["kind"] == "improvement"
    assert row["plan"] is None and row["plan_wo_id"] is None
    assert json.loads(row["metadata"])["evidence_refs"]
