"""Improvement orders: a `feature_orders` row with `kind='improvement'` (docs/superpowers/specs/2026-09-23-improvement-orders.md)"""

from __future__ import annotations

import inspect
import json

import pytest

from jarvis import cli, ops, project_store, steward
from jarvis.catalog import load_catalog
from jarvis.central_store import CentralStore
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


# -- the store: the kind column, and the queries it has to reach

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
        store.create_feature_order("x", description="y", kind="escalation")


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
    """§2.4: the filter goes on the listings, not on the attention path"""
    io = store.create_feature_order("slow first turns", description=OBSERVATION,
                                    kind="improvement")
    store.flag_feature_attention(io["id"], "findings awaiting you")
    assert [r["id"] for r in store.flagged_feature_orders()] == [io["id"]]


def test_the_planner_sweep_never_sees_an_improvement_order(store):
    """The exact call `Daemon.plan_features` makes"""
    io = store.create_feature_order("slow first turns", description=OBSERVATION,
                                    kind="improvement")
    assert io["status"] == "pending"
    assert store.list_feature_orders(statuses=("pending",)) == []


def test_feature_order_for_planner_matches_an_analyst_row(store):
    """§2.4: `feature_order_for_planner` keys on `plan_wo_id` and will now match the analyst too"""
    io = store.create_feature_order("slow first turns", description=OBSERVATION,
                                    kind="improvement")
    wo = store.create_work_order("analyse it", description="…")
    store.update_feature_order(io["id"], plan_wo_id=wo["id"])
    assert store.feature_order_for_planner(wo["id"])["id"] == io["id"]
    assert store.feature_order_for_planner(io["id"])["id"] == io["id"]


def test_no_negative_kind_filter_exists_in_the_new_sql():
    """§2.4 again, from the other side: a negative filter excludes nothing that has not been invented yet"""
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
    assert pred("fo-abc") and pred("io-abc") and pred("inv-abc")
    assert not pred("wo-abc")
    assert not pred("al-abc")


# -- ops: filing one

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


# -- ops: the steward decides a WORKER's io (io-c74f22c3, Q2134)

@pytest.fixture()
def enforcing(jarvis_home, fake_claude, catalog_file, project, self_dispatch_on):
    """`started`, with the steward enforcing — `weekly_usd` is the room it has left"""
    def make(**evolution):
        data = json.loads(catalog_file.read_text())
        data["os"]["evolution"] = {"mode": "enforce", **evolution}
        catalog_file.write_text(json.dumps(data))
        ops.start_os(str(catalog_file), foreground=True)
    return make


def _decisions():
    central = CentralStore()
    try:
        return central.steward_decisions()
    finally:
        central.close()


def test_a_worker_filed_improvement_order_over_budget_is_refused(enforcing, monkeypatch,
                                                                 store):
    enforcing(weekly_usd=0.0)
    monkeypatch.setenv("JARVIS_WO_ID", "wo-3b5590a0")
    with pytest.raises(ops.OpsError) as e:
        ops.create_improvement_order("proj_a", "slow first turns",
                                     description=OBSERVATION, refs=REFS)
    msg = str(e.value)
    assert "queue" in msg and "the evolution ledger is spent" in msg
    assert "jarvis steward decisions" in msg
    assert store.list_feature_orders(kind="improvement", statuses=None) == []
    [row] = _decisions()
    assert (row["origin_kind"], row["outcome"], row["applied"]) == ("worker_io",
                                                                   "queue", 0)


def test_a_worker_filed_improvement_order_with_room_is_stamped_and_budgeted(
        enforcing, monkeypatch, store):
    enforcing(weekly_usd=30.0, order_budget_usd=25.0)
    monkeypatch.setenv("JARVIS_WO_ID", "wo-3b5590a0")
    io = ops.create_improvement_order("proj_a", "slow first turns",
                                      description=OBSERVATION, refs=REFS)
    row = store.get_feature_order(io["id"])
    assert row["budget_usd"] == 25.0
    stamp = json.loads(row["metadata"])[steward.STAMP_KEY]
    assert (stamp["origin_kind"], stamp["severity"]) == ("worker_io", "major")
    # `jarvis steward why <io-id>` answers: the decision names the order it led to
    [decision] = _decisions()
    assert (decision["wo_id"], decision["outcome"]) == (io["id"], "dispatch")


def test_a_worker_filed_improvement_order_needs_self_dispatch_on(started, monkeypatch):
    monkeypatch.setenv("JARVIS_WO_ID", "wo-3b5590a0")
    with pytest.raises(ops.OpsError) as e:
        ops.create_improvement_order("proj_a", "slow first turns",
                                     description=OBSERVATION, refs=REFS)
    assert "`os.autonomy.self_dispatch` is off" in str(e.value)
    assert _decisions()[0]["origin_kind"] == "worker_io"


def test_a_queued_worker_improvement_order_is_kept_and_filed_once_there_is_room(
        enforcing, monkeypatch, store, catalog_file):
    """Decision #61 of 2026-10-22: a `queue` left nothing queued"""
    enforcing(weekly_usd=0.0)
    monkeypatch.setenv("JARVIS_WO_ID", "wo-3b5590a0")
    with pytest.raises(ops.OpsError) as e:
        ops.create_improvement_order("proj_a", "slow first turns",
                                     description=OBSERVATION, refs=REFS)
    assert "kept whole" in str(e.value) and "Do not file it again" in str(e.value)
    [row] = _decisions()
    assert row["queue_state"] == "waiting"
    assert json.loads(row["candidate_json"])["payload"]["description"] == OBSERVATION
    central = CentralStore()
    try:
        assert steward.drain(ops.resolve_catalog(), central)["waiting"] == 1
        assert store.list_feature_orders(kind="improvement", statuses=None) == []
        enforcing(weekly_usd=30.0, order_budget_usd=25.0)
        assert steward.drain(ops.resolve_catalog(), central)["dispatched"] == 1
    finally:
        central.close()
    [io] = store.list_feature_orders(kind="improvement", statuses=None)
    assert (io["title"], io["description"], io["budget_usd"]) == (
        "slow first turns", OBSERVATION, 25.0)
    meta = json.loads(io["metadata"])
    assert meta[ops.EVIDENCE_REFS_KEY] == list(REFS)
    assert meta[steward.STAMP_KEY]["decision_id"] == row["id"]
    [row] = _decisions()
    assert (row["queue_state"], row["wo_id"], row["outcome"]) == ("reoffered", io["id"],
                                                                 "dispatch")


def test_a_worker_filed_improvement_order_fails_closed_on_an_unreadable_catalog(
        started, monkeypatch, store):
    """Decisions #60/#62 of 2026-10-22: an unreadable catalog let a worker's io through"""
    def unreadable():
        raise ops.OpsError("catalog unreadable")
    monkeypatch.setattr(ops, "resolve_catalog", unreadable)
    monkeypatch.setenv("JARVIS_WO_ID", "wo-3b5590a0")
    with pytest.raises(ops.OpsError) as e:
        ops.create_improvement_order("proj_a", "slow first turns",
                                     description=OBSERVATION, refs=REFS)
    msg = str(e.value)
    assert "could not read the catalog" in msg and "catalog unreadable" in msg
    assert store.list_feature_orders(kind="improvement", statuses=None) == []
    # The user's own io does not depend on the steward, so it still files
    monkeypatch.delenv("JARVIS_WO_ID")
    assert ops.create_improvement_order("proj_a", "slow first turns",
                                        description=OBSERVATION, refs=REFS)["id"]


def test_the_users_own_improvement_order_is_not_decided_by_the_steward(enforcing,
                                                                      monkeypatch, store):
    enforcing(weekly_usd=0.0)
    monkeypatch.delenv("JARVIS_WO_ID", raising=False)
    io = ops.create_improvement_order("proj_a", "slow first turns",
                                      description=OBSERVATION, refs=REFS)
    assert steward.STAMP_KEY not in json.loads(
        store.get_feature_order(io["id"])["metadata"])
    assert _decisions() == []


# -- ops: the listings, the show, the guards

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


def test_fo_show_refuses_an_improvement_order_and_names_io_show(started):
    """GitHub issue #997: `show_feature_order` was the one `show_*` with no kind guard"""
    io = ops.create_improvement_order("proj_a", "slow first turns",
                                      description=OBSERVATION, refs=REFS)
    with pytest.raises(ops.OpsError) as e:
        ops.show_feature_order(io["id"])
    assert "jarvis io show" in str(e.value)
    assert "an improvement order" in str(e.value)


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


# -- the budget passthrough

def test_the_family_budget_works_on_an_io_id(started):
    io = ops.create_improvement_order("proj_a", "slow first turns",
                                      description=OBSERVATION, refs=REFS,
                                      budget_usd=4.0)
    assert ops.feature_order_budget(io["id"])["budget_usd"] == 4.0
    assert ops.set_feature_budget(io["id"], 9.0)["budget_usd"] == 9.0
    assert ops.feature_order_budget(io["id"])["budget_usd"] == 9.0
    assert ops.set_feature_budget(io["id"], None)["budget_usd"] is None


# -- the CLI

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


# -- the shared fixture (§2.7)

# -- the kind-agnostic paths (2026-10-01-a-family-capped-raise-must-say-so.md)


def test_the_io_budget_verbs_refuse_a_row_of_another_kind(started):
    """Obligation 5"""
    fo = ops.create_feature_order("proj_a", "CSV export", description="the whole ask")
    for call in (lambda i: ops.improvement_order_budget(i),
                 lambda i: ops.set_improvement_budget(i, 9.0)):
        with pytest.raises(ops.OpsError) as e:
            call(fo["id"])
        assert "jarvis fo budget" in str(e.value)

    io = ops.create_improvement_order("proj_a", "slow first turns",
                                      description=OBSERVATION, refs=REFS, budget_usd=4.0)
    assert ops.improvement_order_budget(io["id"])["budget_usd"] == 4.0
    assert ops.set_improvement_budget(io["id"], 9.0)["budget_usd"] == 9.0


def test_raising_the_budget_names_the_parked_analyst(started, store):
    """Obligation 5, the half that was silent (Neo question 1198)"""
    io = ops.create_improvement_order("proj_a", "slow first turns",
                                      description=OBSERVATION, refs=REFS, budget_usd=2.0)
    child = store.create_work_order("analyse it", description="look",
                                    kind="analyst", parent_id=io["id"])
    store.update_feature_order(io["id"], plan_wo_id=child["id"])
    store.update_work_order(child["id"], budget_reserved_usd=2.0,
                            status="budget_exhausted")

    out = ops.set_improvement_budget(io["id"], 20.0)
    assert out["exhausted_children"] == [child["id"]]


def test_a_settled_flagged_improvement_order_still_lists(started, store):
    """Obligation 6"""
    io = ops.create_improvement_order("proj_a", "slow first turns",
                                      description=OBSERVATION, refs=REFS)
    quiet = ops.create_improvement_order("proj_a", "another one",
                                         description=OBSERVATION, refs=REFS)
    store.set_feature_status(io["id"], "completed")
    store.flag_feature_attention(io["id"], "findings awaiting you")
    store.set_feature_status(quiet["id"], "completed")

    listed = [r["id"] for r in ops.list_improvement_orders("proj_a")]
    assert io["id"] in listed
    assert quiet["id"] not in listed          # settled and unflagged stays hidden
    assert quiet["id"] in [r["id"] for r in
                           ops.list_improvement_orders("proj_a", include_settled=True)]
    # ...and the flagged io is still not in the FEATURE listing: this is not a kind leak
    assert io["id"] not in [r["id"] for r in ops.list_feature_orders("proj_a")]


def test_os_status_carries_every_kind_in_one_list(started, store):
    """Obligation 7"""
    fo = ops.create_feature_order("proj_a", "CSV export", description="the whole ask")
    io = ops.create_improvement_order("proj_a", "slow first turns",
                                      description=OBSERVATION, refs=REFS)
    store.set_feature_status(io["id"], "planning")

    rows = ops.os_status()["projects"][0]["feature_orders"]
    by_id = {r["id"]: r for r in rows}
    assert by_id[fo["id"]]["kind"] == "feature"
    assert by_id[io["id"]]["kind"] == "improvement"
    # A kind-aware label, through the one mapping: `planning` means "analysing" here
    assert by_id[io["id"]]["status_label"] == "analysing"
    assert by_id[fo["id"]]["status_label"] == "pending"


def test_the_two_settle_passes_name_their_kind_explicitly():
    """Obligation 9"""
    src = inspect.getsource(Daemon.settle_features)
    assert 'statuses=("executing",), kind="feature"' in src
    # The budget pass drives its kind off the table of what each kind runs in, so it reaches all three
    assert "_FAMILY_RUNNING_STATUS.items()" in src
    assert src.count('kind="feature"') == 1


def test_the_shared_fixture_files_one(improvement_order, store):
    row = store.get_feature_order(improvement_order["id"])
    assert row["kind"] == "improvement"
    assert row["plan"] is None and row["plan_wo_id"] is None
    assert json.loads(row["metadata"])["evidence_refs"]
