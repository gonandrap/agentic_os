"""`jarvis wo defer`: a work order hands off work it should not do, and forgets it"""

from __future__ import annotations

import pytest

from jarvis import bus, cli, dispatch, ops
from jarvis.catalog import load_catalog
from jarvis.central_store import CentralStore
from jarvis.daemon import Daemon
from jarvis.project_store import ProjectStore


@pytest.fixture()
def store(project, jarvis_home):
    """The project's own store, with the project registered so `jarvis wo defer`"""
    central = CentralStore()
    try:
        central.upsert_project("proj_a", str(project), "test project")
    finally:
        central.close()
    s = ProjectStore(project)
    yield s
    s.close()


@pytest.fixture()
def central(jarvis_home):
    c = CentralStore()
    yield c
    c.close()


@pytest.fixture()
def tick(catalog_file):
    """One reconcile tick's worth of envelope routing, and nothing else"""
    daemon = Daemon(load_catalog(catalog_file))
    spec = load_catalog(catalog_file).projects[0]

    def _tick(store):
        daemon.deliver_envelopes(spec, store)

    return _tick


def a_work_order(store, title="implement the thing", **kw):
    return store.create_work_order(title, "do it", **kw)["id"]


def a_manager(store, fo_id, status="waiting_input"):
    wo_id = a_work_order(store, "own the feature", parent_id=fo_id, kind="manager")
    store.set_status(wo_id, status)
    return wo_id


def a_feature_child(store, *, with_manager: bool):
    """A work order under a feature order, with or without a project manager over it"""
    fo_id = store.create_feature_order("ship the exporter")["id"]
    manager = a_manager(store, fo_id) if with_manager else None
    return fo_id, manager, a_work_order(store, "build half of it", parent_id=fo_id)


def envelopes(store):
    return store.envelopes()


def messages(store, wo_id):
    return [m for m in store.list_messages(wo_id) if m["direction"] == "user_to_agent"]


# -- what the command does, and what the router does with it


def test_a_deferral_with_no_manager_is_filed_by_the_router_and_one_with_a_manager_is_not(
        store, central, tick):
    """The two halves of the routing rule, in one test on purpose"""
    orphan = a_work_order(store, "a standalone work order")
    ops.defer(orphan, "rate limit the exporter",
              "out of scope for this work order, and nothing breaks without it")
    _, manager, child = a_feature_child(store, with_manager=True)
    ops.defer(child, "cache the catalog read", "noticed on the way past")

    tick(store)

    # No manager: the ROUTER files it, and tells nobody
    filed = [b for b in central.list_backlog("proj_a", status=None)
             if b["title"] == "rate limit the exporter"]
    assert len(filed) == 1
    assert filed[0]["status"] == "proposed"  # the steward reviews it (spec 2026-11-03 §4.4)
    assert [r["kind"] for r in filed[0]["origin_refs"]] == ["envelope", "order"]
    assert filed[0]["origin_refs"][0]["subject_wo_id"] == orphan
    assert filed[0]["origin_wo_id"] == orphan
    assert "nothing breaks without it" in filed[0]["origin_note"]
    assert not messages(store, orphan)
    orphan_env = next(e for e in envelopes(store) if e["subject_wo_id"] == orphan)
    assert orphan_env["state"] == "handled_by_router"
    assert orphan_env["delivered_wo_id"] is None

    # A manager: it is the manager's job, so the router files NOTHING itself
    assert not [b for b in central.list_backlog("proj_a", status=None)
                if b["title"] == "cache the catalog read"]
    queued = messages(store, manager)
    assert len(queued) == 1
    assert "cache the catalog read" in queued[0]["content"]
    child_env = next(e for e in envelopes(store) if e["subject_wo_id"] == child)
    assert child_env["state"] == "delivered"
    assert child_env["delivered_wo_id"] == manager


def test_defer_does_not_touch_the_backlog_or_the_router(store, monkeypatch):
    """THE decoupling test"""
    def boom(*a, **k):
        raise AssertionError("the sender must not call this")

    monkeypatch.setattr(CentralStore, "add_backlog", boom)
    monkeypatch.setattr(bus, "resolve", boom)

    wo_id = a_work_order(store)
    out = ops.defer(wo_id, "split the exporter", "too big to finish here")

    assert out["wo_id"] == wo_id
    posted = envelopes(store)
    assert len(posted) == 1
    assert posted[0]["state"] == "queued"
    assert posted[0]["to_role"] == "manager"
    assert posted[0]["kind"] == "deferral_request"
    assert posted[0]["delivered_wo_id"] is None
    # And it says nothing about what happened next, because it must not depend on it
    assert "backlog" not in out["note"]


def test_the_command_names_no_recipient_and_reads_no_parent(store, monkeypatch):
    """`defer` must not look at whether this work order has a feature over it: that is the branch the..."""
    _, _, child = a_feature_child(store, with_manager=True)

    def boom(*a, **k):
        raise AssertionError("the sender must not resolve a role")

    monkeypatch.setattr(ProjectStore, "manager_work_order", boom)
    ops.defer(child, "rework the retry policy", "belongs with the retry work")

    posted = envelopes(store)[0]
    assert posted["state"] == "queued"
    assert posted["delivered_wo_id"] is None


def test_with_validation_off_a_feature_child_takes_the_router_path_too(
        store, central, tick):
    """`os.validation.enabled` false means no manager order is ever created"""
    fo_id, manager, child = a_feature_child(store, with_manager=False)
    assert manager is None

    ops.defer(child, "document the exporter", "no user-facing docs yet")
    tick(store)

    item = central.list_backlog("proj_a", status=None)[0]
    assert item["origin_wo_id"] == child
    assert item["origin_fo_id"] == fo_id  # the plan it came out of, manager or not
    assert next(iter(envelopes(store)))["state"] == "handled_by_router"


# -- the relationship, whichever path files it


def test_both_filing_paths_record_the_same_relationship(store, central, tick, monkeypatch):
    """One deferral filed by the router and one filed the way the manager is told to"""
    fo_a, _, child_a = a_feature_child(store, with_manager=False)
    ops.defer(child_a, "rate limit the exporter", "out of scope here",
              neo_question_id=41)
    tick(store)
    by_router = central.list_backlog("proj_a", status=None)[0]

    fo_b, manager, child_b = a_feature_child(store, with_manager=True)
    ops.defer(child_b, "rate limit the importer", "out of scope here",
              neo_question_id=41)
    tick(store)
    told = messages(store, manager)[0]["content"]

    # What the manager is handed is the command itself, values already in it
    assert "jarvis backlog propose proj_a" in told
    assert f"--origin-wo {child_b}" in told
    assert f"--origin-fo {fo_b}" in told
    assert "--origin-note" in told
    assert "--ref q41" in told
    # the manager runs it from its own session: the explicit origin beats the session's
    monkeypatch.setenv("JARVIS_WO_ID", manager)
    monkeypatch.setenv("JARVIS_PROJECT", "proj_a")
    monkeypatch.setattr("jarvis.neo_store.NeoStore.get", lambda self, qid: {"id": qid})
    assert cli.main(["backlog", "propose", "proj_a", "rate limit the importer",
                     "--type", "task", "--severity", "medium", "--ref", "q41",
                     "--origin-wo", child_b, "--origin-fo", fo_b,
                     "--origin-note", bus.origin_note(
                         bus.DeferralRequest("rate limit the importer",
                                             "out of scope here", 41))]) == 0
    by_manager = [b for b in central.list_backlog("proj_a", status=None)
                  if b["title"] == "rate limit the importer"][0]

    assert by_router["status"] == by_manager["status"] == "proposed"
    assert (by_router["origin_wo_id"], by_router["origin_fo_id"]) == (child_a, fo_a)
    assert (by_manager["origin_wo_id"], by_manager["origin_fo_id"]) == (child_b, fo_b)
    assert [r["kind"] for r in by_manager["origin_refs"]] == ["turn", "neo_question"]
    assert by_manager["origin_refs"][0]["wo_id"] == manager
    assert by_router["origin_note"] == by_manager["origin_note"]
    assert by_router["origin_note"] == "out of scope here (Neo question 41)"


def test_a_neo_question_id_lands_in_the_origin_note(store, central, tick):
    """Paired with a deferral that agreed nothing with Neo: the note has to stay clean for the common..."""
    with_q = a_work_order(store, "one that asked")
    without = a_work_order(store, "one that did not")
    ops.defer(with_q, "rate limit the exporter", "Neo agreed it is a separate job",
              neo_question_id=137)
    ops.defer(without, "cache the catalog read", "nobody had to decide this")

    tick(store)

    notes = {b["title"]: b["origin_note"] for b in central.list_backlog("proj_a", status=None)}
    assert notes["rate limit the exporter"] == (
        "Neo agreed it is a separate job (Neo question 137)")
    assert notes["cache the catalog read"] == "nobody had to decide this"


def test_defer_refuses_a_deferral_with_no_argument_for_it(store):
    wo_id = a_work_order(store)
    with pytest.raises(ops.OpsError):
        ops.defer(wo_id, "rate limit the exporter", "   ")
    assert not envelopes(store)


def test_the_deferral_is_on_the_work_orders_own_record(store):
    """The backlog item carries a pointer to the work order"""
    from jarvis import timeline

    wo_id = a_work_order(store)
    ops.defer(wo_id, "rate limit the exporter", "out of scope for this work order")

    entries = timeline.build_timeline(store.get_work_order(wo_id),
                                      store.list_events(wo_id),
                                      store.list_messages(wo_id))
    deferral = [e for e in entries if e["kind"] == "deferral_submitted"]
    assert len(deferral) == 1
    assert deferral[0]["label"] != "deferral_submitted"  # classified, not a raw blob
    assert "rate limit the exporter" in deferral[0]["detail"]


# -- the backlog itself


def test_every_existing_add_backlog_caller_is_unaffected(central):
    """Called exactly as every caller that predates deferral routing calls it"""
    item = central.add_backlog("proj_a", "something somebody typed",
                               description="with a description")

    assert item["origin_wo_id"] is None
    assert item["origin_fo_id"] is None
    assert item["origin_note"] == ""
    assert central.get_backlog(item["id"])["origin_wo_id"] is None
    assert central.list_backlog("proj_a", status=None)[0]["origin_note"] == ""


def test_backlog_list_shows_an_origin_and_leaves_a_typed_item_alone(
        store, tick, capsys):
    """Paired: the origin line is an ADDITION to the listing"""
    assert cli.main(["backlog", "add", "proj_a", "something somebody typed"]) == 0
    wo_id = a_work_order(store)
    ops.defer(wo_id, "rate limit the exporter", "out of scope for this work order")
    tick(store)
    capsys.readouterr()

    assert cli.main(["backlog", "list", "proj_a", "--all"]) == 0  # the router's is proposed
    lines =[ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]

    typed = [ln for ln in lines if "something somebody typed" in ln]
    assert len(typed) == 1
    assert typed[0].startswith("• ")
    assert lines[lines.index(typed[0]) + 1].startswith("• ")  # nothing added under it

    deferred = next(ln for ln in lines if "rate limit the exporter" in ln)
    origin = lines[lines.index(deferred) + 1]
    assert origin.strip().startswith("↳")
    assert wo_id in origin
    assert "out of scope for this work order" in origin


def test_backlog_show_renders_the_origin(store, central, tick, capsys):
    wo_id = a_work_order(store)
    ops.defer(wo_id, "rate limit the exporter", "out of scope for this work order")
    tick(store)
    item = central.list_backlog("proj_a", status=None)[0]
    capsys.readouterr()

    assert cli.main(["backlog", "show", item["id"]]) == 0
    out = capsys.readouterr().out
    assert wo_id in out
    assert "out of scope for this work order" in out

    plain = central.add_backlog("proj_a", "something somebody typed")
    assert cli.main(["backlog", "show", plain["id"]]) == 0
    assert "origin" not in capsys.readouterr().out


def test_add_from_a_work_order_is_a_proposal_and_from_the_user_is_open(
        store, central, monkeypatch, capsys):
    """Spec 2026-11-03-backlog-operability §4.3, paired so neither half passes alone"""
    assert cli.main(["backlog", "add", "proj_a", "the user typed it"]) == 0
    fo_id, _, wo_id = a_feature_child(store, with_manager=False)
    monkeypatch.setenv("JARVIS_WO_ID", wo_id)
    monkeypatch.setenv("JARVIS_PROJECT", "proj_a")
    capsys.readouterr()
    assert cli.main(["backlog", "add", "proj_a", "a worker typed it"]) == 0
    assert "proposed" in capsys.readouterr().out

    rows = {b["title"]: b for b in central.list_backlog("proj_a", status=None)}
    assert rows["the user typed it"]["status"] == "open"
    assert rows["the user typed it"]["origin_refs"] == []
    worker = rows["a worker typed it"]
    assert worker["status"] == "proposed"
    assert (worker["item_type"], worker["severity"]) == ("task", "medium")
    assert (worker["origin_wo_id"], worker["origin_fo_id"]) == (wo_id, fo_id)
    assert worker["origin_refs"][0]["kind"] == "turn"
    assert worker["origin_refs"][0]["wo_id"] == wo_id


def test_propose_refuses_a_ref_that_does_not_exist_and_keeps_one_that_does(
        store, central, monkeypatch):
    """§4.2: a missing id is refused, not stored; paired with a real one"""
    wo_id = a_work_order(store)
    monkeypatch.setenv("JARVIS_WO_ID", wo_id)
    monkeypatch.setenv("JARVIS_PROJECT", "proj_a")
    base = ["backlog", "propose", "proj_a", "x", "--type", "bug", "--severity", "major"]
    assert cli.main([*base, "--ref", "wo-doesnotexist"]) != 0
    assert cli.main([*base, "--ref", "not-a-ref"]) != 0
    assert central.list_backlog("proj_a", status=None) == []

    assert cli.main([*base, "--ref", wo_id]) == 0
    [row] = central.list_backlog("proj_a", status=None)
    assert row["origin_refs"][1] == {"kind": "order", "id": wo_id}
    assert (row["item_type"], row["severity"], row["status"]) == ("bug", "major", "proposed")


def test_propose_requires_type_and_severity(store):
    with pytest.raises(SystemExit):
        cli.main(["backlog", "propose", "proj_a", "x", "--type", "task"])
    with pytest.raises(SystemExit):
        cli.main(["backlog", "propose", "proj_a", "x", "--severity", "minor"])


# -- the two contracts


def test_the_worker_is_told_to_defer_rather_than_to_file_it_itself():
    """The path needs callers, or it ships untested"""
    from jarvis import worker_brief

    text = worker_brief.render_section("contract", wo_id="wo-936a13ca", project="proj_a")
    assert "jarvis wo defer wo-936a13ca" in text
    assert "--why" in text
    assert "jarvis backlog add proj_a" not in text


# -- the manager's contract


def test_the_manager_is_told_to_file_it_with_the_relationship(project, catalog_file):
    """The prose has to name the invocation"""
    store = ProjectStore(project)
    try:
        fo_id = store.create_feature_order("ship the exporter")["id"]
        wo = store.create_work_order("own the feature", "coordinate it",
                                     parent_id=fo_id, kind="manager")
        prompt = dispatch.build_worker_prompt(
            wo, load_catalog(catalog_file).projects[0],
            feature={"fo": store.get_feature_order(fo_id), "children": []})
    finally:
        store.close()

    assert "--origin-wo" in prompt
    assert f"--origin-fo {fo_id}" in prompt
    assert "--origin-note" in prompt
    # It does not learn who sends it anything: no seat, no panel, no validator
    low = prompt.lower()
    assert "panel" not in low
    assert "validator" not in low
    assert "seat" not in low
