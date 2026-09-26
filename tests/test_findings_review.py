"""`ops.review_findings` — the user deciding a findings report, finding by finding.

Section 5 of docs/superpowers/specs/2026-09-23-improvement-orders.md. Four properties
carry the section and each has tests here:

* an accepted finding writes exactly ONE knowledge entry, through `ops.learn_add`, and
  files its proposed orders with `parent_id` NULL and an `origin_io` back-link readable
  from both ends;
* the DECISION and the knowledge write are durable and the filing is best effort
  (kn-652456b8) — but a filing failure is never silent: the flag stays up, an inbox row
  is raised, and the order stays in `plan_review` for a retry;
* a retry files what did not file and writes no second knowledge entry;
* every finding decided completes the order, including an order whose findings were all
  rejected.
"""

from __future__ import annotations

import json as _json

import pytest

from jarvis import cli, db, findings, ops, timeline
from jarvis.catalog import load_catalog
from jarvis.central_store import CentralStore
from jarvis.daemon import Daemon
from jarvis.neo_store import NeoStore
from jarvis.project_store import ProjectStore
from jarvis.testing import a_finding, a_report, make_git_project


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


@pytest.fixture()
def store(project):
    s = ProjectStore(project)
    yield s
    s.close()


@pytest.fixture()
def register_proj_b(tmp_path, claude_json, catalog_file):
    """Register a SECOND project, on demand: a proposal's fix is frequently elsewhere."""
    def register():
        other = make_git_project(tmp_path, "proj_b")
        claude_json(other)
        data = _json.loads(catalog_file.read_text())
        data["projects"].append({"name": "proj_b", "path": str(other),
                                 "description": "the other project"})
        catalog_file.write_text(_json.dumps(data))
        ops.start_os(str(catalog_file), foreground=True)
        return other
    return register


@pytest.fixture()
def reviewable(started, store, improvement_order):
    """An improvement order in `plan_review` with the shared report stored on it."""
    analyst = store.create_work_order("analyse it", description="the observation",
                                      kind="planner", status="running")
    store.update_feature_order(improvement_order["id"], plan_wo_id=analyst["id"],
                               status="planning")
    ops.submit_findings(improvement_order["id"], a_report())
    return improvement_order, analyst


def stored_report(store, io_id):
    return db.from_json(store.get_feature_order(io_id)["plan"], None)


def finding_named(store, io_id, key):
    return next(f for f in stored_report(store, io_id)["findings"] if f["key"] == key)


def knowledge_entries():
    central = CentralStore()
    try:
        return central.search_knowledge("", limit=200)
    finally:
        central.close()


def inbox_rows():
    central = CentralStore()
    try:
        return central.unacked_inbox()
    finally:
        central.close()


def learnings():
    neo = NeoStore()
    try:
        return neo.all_learnings()
    finally:
        neo.close()


# -- accepting -------------------------------------------------------------------------

def test_accepting_writes_one_knowledge_entry_and_files_the_proposed_order(reviewable,
                                                                          store):
    io, analyst = reviewable

    out = ops.review_findings(io["id"], accept=["first-turn-reads"])

    entries = [k for k in knowledge_entries() if k["topic"] == "os-failure-mode"]
    assert len(entries) == 1
    entry = entries[0]
    assert entry["project"] == "proj_a"
    assert entry["tags"] == io["id"]
    assert entry["wo_id"] == analyst["id"]
    # FIRST LINE is the root cause as a rule — it is the only part a worker's prompt
    # index carries (§5.4).
    assert entry["content"].splitlines()[0].startswith("The dispatch brief names no "
                                                       "entry point")
    assert out["learnings"] == [entry["id"]]

    assert len(out["created"]) == 1
    filed = out["created"][0]
    assert filed["type"] == "work"
    _name, _path, wo = ops.find_work_order(filed["id"])
    assert wo["parent_id"] is None
    assert db.from_json(wo["metadata"], {})[ops.ORIGIN_IO_KEY] == io["id"]
    assert io["id"] in wo["description"].splitlines()[0]
    assert f"jarvis io show {io['id']}" in wo["description"].splitlines()[0]
    assert "Add the owning module" in wo["description"]

    finding = finding_named(store, io["id"], "first-turn-reads")
    assert finding["status"] == "accepted"
    assert finding["decided_by"] == "user"
    assert finding["decided_at"]
    assert finding["knowledge_id"] == entry["id"]
    assert [o["id"] for o in finding["created_orders"]] == [filed["id"]]


def test_a_feature_proposal_becomes_a_feature_order_with_no_parent(reviewable, store):
    io, _ = reviewable
    report = stored_report(store, io["id"])
    report["findings"][0]["proposed_orders"] = [{
        "type": "feature", "project": "proj_a", "title": "regenerate the code map",
        "description": ("Regenerate the committed code map from the symbol index on "
                        "every release and fail the release when the two disagree."),
    }]
    store.update_feature_order(io["id"], plan=db.to_json(report))

    out = ops.review_findings(io["id"], accept=["first-turn-reads"])

    assert out["created"][0]["type"] == "feature"
    _name, _path, fo = ops.find_feature_order(out["created"][0]["id"])
    assert fo["kind"] == "feature"
    assert db.from_json(fo["metadata"], {})[ops.ORIGIN_IO_KEY] == io["id"]
    assert io["id"] in fo["description"].splitlines()[0]


def test_a_cross_project_proposal_lands_in_the_other_project(reviewable, store,
                                                             register_proj_b):
    io, _ = reviewable
    register_proj_b()
    report = stored_report(store, io["id"])
    report["findings"][0]["proposed_orders"][0]["project"] = "proj_b"
    store.update_feature_order(io["id"], plan=db.to_json(report))

    out = ops.review_findings(io["id"], accept=["first-turn-reads"])

    name, _path, _wo = ops.find_work_order(out["created"][0]["id"])
    assert name == "proj_b"
    assert finding_named(store, io["id"],
                         "first-turn-reads")["created_orders"][0]["project"] == "proj_b"


def test_a_blank_project_on_a_proposal_falls_back_to_the_orders_own(reviewable, store):
    io, _ = reviewable
    report = stored_report(store, io["id"])
    report["findings"][0]["proposed_orders"][0]["project"] = ""
    store.update_feature_order(io["id"], plan=db.to_json(report))

    out = ops.review_findings(io["id"], accept=["first-turn-reads"])

    assert ops.find_work_order(out["created"][0]["id"])[0] == "proj_a"


# -- completion ------------------------------------------------------------------------

def test_every_finding_decided_completes_the_order_and_clears_the_flag(reviewable,
                                                                      store):
    io, analyst = reviewable

    out = ops.review_findings(io["id"], accept=["first-turn-reads"],
                              reject={"stale-code-map": "the map is about to be deleted"})

    assert out["status"] == "completed"
    row = store.get_feature_order(io["id"])
    assert row["status"] == "completed"
    assert not row["needs_attention"]
    assert row["attention_reason"] is None
    assert store.events_of_kind(analyst["id"], "findings_reviewed")


def test_an_all_rejected_order_still_completes(reviewable, store):
    io, _ = reviewable

    out = ops.review_findings(io["id"], reject={
        "first-turn-reads": "the brief already names it; the workers ignore it",
        "stale-code-map": "the map is about to be deleted",
    })

    assert out["status"] == "completed"
    assert store.get_feature_order(io["id"])["status"] == "completed"
    assert not [k for k in knowledge_entries() if k["topic"] == "os-failure-mode"]
    assert not out["created"]


def test_a_partial_decision_leaves_it_open_with_the_headline_recomputed(reviewable,
                                                                       store):
    io, _ = reviewable

    out = ops.review_findings(io["id"], accept=["first-turn-reads"])

    assert out["status"] == "plan_review"
    row = store.get_feature_order(io["id"])
    assert row["status"] == "plan_review"
    assert row["needs_attention"]
    assert row["attention_reason"] == findings.review_headline(
        row, stored_report(store, io["id"]))
    assert "1 accepted" in row["attention_reason"]
    assert "1 awaiting you" in row["attention_reason"]


def test_accept_all_takes_every_pending_finding(reviewable, store):
    io, _ = reviewable

    out = ops.review_findings(io["id"], accept_all=True)

    assert out["status"] == "completed"
    assert [f["status"] for f in stored_report(store, io["id"])["findings"]] == \
        ["accepted", "accepted"]


# -- rejecting -------------------------------------------------------------------------

def test_a_rejection_teaches_neo_and_writes_no_knowledge_entry(reviewable, store):
    io, _ = reviewable
    reason = "the brief already names the entry point; the workers read past it"

    out = ops.review_findings(io["id"], reject={"first-turn-reads": reason})

    rows = [row for row in learnings() if row["source"] == "review"]
    assert len(rows) == 1
    assert reason in rows[0]["content"]
    assert "The dispatch brief names no entry point" in rows[0]["content"]
    assert rows[0]["project"] == "proj_a"
    assert not knowledge_entries()
    assert out["rejected"] == ["first-turn-reads"]

    finding = finding_named(store, io["id"], "first-turn-reads")
    assert finding["status"] == "rejected"
    assert finding["feedback"] == reason
    assert "knowledge_id" not in finding
    # §5.5: nothing else is built Neo-side.
    assert store.get_feature_order(io["id"])["plan_question_id"] is None


# -- refusals --------------------------------------------------------------------------

def test_a_feature_order_is_refused_and_named_for_the_verb_that_works(started, store):
    fo = store.create_feature_order("CSV export", description="the whole ask")
    store.update_feature_order(fo["id"], status="plan_review")

    with pytest.raises(ops.OpsError) as e:
        ops.review_findings(fo["id"], accept=["x"])

    assert "jarvis fo approve" in str(e.value)


@pytest.mark.parametrize("status", ["planning", "completed", "executing", "cancelled"])
def test_every_other_status_is_refused_with_the_status_named(reviewable, store, status):
    io, _ = reviewable
    store.update_feature_order(io["id"], status=status)

    with pytest.raises(ops.OpsError) as e:
        ops.review_findings(io["id"], accept=["first-turn-reads"])

    assert status in str(e.value)


def test_an_order_with_no_report_is_refused(started, store, improvement_order):
    store.update_feature_order(improvement_order["id"], status="plan_review")

    with pytest.raises(ops.OpsError) as e:
        ops.review_findings(improvement_order["id"], accept=["first-turn-reads"])

    assert "no stored findings report" in str(e.value)


def test_accept_all_with_a_rejection_is_refused(reviewable, store):
    io, _ = reviewable

    with pytest.raises(ops.OpsError) as e:
        ops.review_findings(io["id"], accept_all=True,
                            reject={"stale-code-map": "not worth it"})

    assert "--accept-all" in str(e.value)
    assert [f["status"] for f in stored_report(store, io["id"])["findings"]] == \
        ["pending", "pending"]


def test_an_unknown_key_is_refused_and_the_known_ones_listed(reviewable, store):
    io, _ = reviewable

    with pytest.raises(ops.OpsError) as e:
        ops.review_findings(io["id"], accept=["no-such-finding"])

    message = str(e.value)
    assert "no-such-finding" in message
    assert "first-turn-reads" in message and "stale-code-map" in message


def test_a_key_in_both_accept_and_reject_is_refused(reviewable):
    io, _ = reviewable

    with pytest.raises(ops.OpsError) as e:
        ops.review_findings(io["id"], accept=["first-turn-reads"],
                            reject={"first-turn-reads": "no"})

    assert "first-turn-reads" in str(e.value)


def test_a_rejection_with_no_feedback_is_refused(reviewable, store):
    io, _ = reviewable

    with pytest.raises(ops.OpsError) as e:
        ops.review_findings(io["id"], reject={"first-turn-reads": "   "})

    assert "--feedback" in str(e.value)
    assert not learnings()


def test_a_finding_already_decided_is_refused(reviewable, store):
    io, _ = reviewable
    ops.review_findings(io["id"], accept=["first-turn-reads"])

    with pytest.raises(ops.OpsError) as e:
        ops.review_findings(io["id"], accept=["first-turn-reads"])

    assert "already" in str(e.value)
    assert len([k for k in knowledge_entries()
                if k["topic"] == "os-failure-mode"]) == 1


# -- the filing failure, and the retry --------------------------------------------------

def _proposal_into_an_unregistered_project(store, io_id):
    report = stored_report(store, io_id)
    report["findings"][0]["proposed_orders"][0]["project"] = "proj_b"
    store.update_feature_order(io_id, plan=db.to_json(report))


def test_a_filing_failure_keeps_the_decision_and_raises_the_user(reviewable, store):
    io, _ = reviewable
    _proposal_into_an_unregistered_project(store, io["id"])

    out = ops.review_findings(io["id"], accept=["first-turn-reads"],
                              reject={"stale-code-map": "the map is about to go"})

    assert out["errors"] and "proj_b" in out["errors"][0]["error"]
    assert not out["created"]
    # The decision and the knowledge entry are DURABLE (kn-652456b8).
    finding = finding_named(store, io["id"], "first-turn-reads")
    assert finding["status"] == "accepted"
    assert finding["knowledge_id"]
    assert "proj_b" in finding["filing_error"]
    assert len([k for k in knowledge_entries() if k["topic"] == "os-failure-mode"]) == 1

    row = store.get_feature_order(io["id"])
    assert row["status"] == "plan_review"          # no finding is pending; the filing is
    assert out["status"] == "plan_review"
    assert row["needs_attention"]
    assert "first-turn-reads" in row["attention_reason"]
    assert "proj_b" in row["attention_reason"]
    assert f"jarvis io review {io['id']}" in row["attention_reason"]

    warnings = [r for r in inbox_rows() if r["level"] == "warning"]
    assert len(warnings) == 1
    assert "first-turn-reads" in (warnings[0]["title"] + warnings[0]["body"])


def test_retrying_the_accept_files_it_and_writes_no_second_entry(reviewable, store,
                                                                 register_proj_b):
    io, _ = reviewable
    _proposal_into_an_unregistered_project(store, io["id"])
    ops.review_findings(io["id"], accept=["first-turn-reads"],
                        reject={"stale-code-map": "the map is about to go"})
    first_entry = finding_named(store, io["id"], "first-turn-reads")["knowledge_id"]
    register_proj_b()

    out = ops.review_findings(io["id"], accept=["first-turn-reads"])

    assert len(out["created"]) == 1
    assert not out["errors"]
    assert out["learnings"] == []
    assert out["status"] == "completed"
    entries = [k for k in knowledge_entries() if k["topic"] == "os-failure-mode"]
    assert len(entries) == 1 and entries[0]["id"] == first_entry

    finding = finding_named(store, io["id"], "first-turn-reads")
    assert "filing_error" not in finding
    assert len(finding["created_orders"]) == 1
    row = store.get_feature_order(io["id"])
    assert row["status"] == "completed"
    assert not row["needs_attention"]


def test_a_retry_files_the_order_and_changes_nothing_else_about_the_decision(
        reviewable, store, register_proj_b):
    """§5.2: the decision STANDS and only the filing is retried."""
    io, _ = reviewable
    _proposal_into_an_unregistered_project(store, io["id"])
    ops.review_findings(io["id"], accept=["first-turn-reads"],
                        feedback="file it in proj_b, that is where the brief is built")
    before = finding_named(store, io["id"], "first-turn-reads")
    register_proj_b()

    ops.review_findings(io["id"], accept=["first-turn-reads"])

    after = finding_named(store, io["id"], "first-turn-reads")
    assert after["feedback"] == before["feedback"]
    assert after["decided_at"] == before["decided_at"]
    assert after["decided_by"] == before["decided_by"]
    assert after["knowledge_id"] == before["knowledge_id"]
    assert len(after["created_orders"]) == 1


# -- what a worker actually sees -----------------------------------------------------------

def test_the_knowledge_entrys_first_line_is_the_root_cause_and_nothing_else():
    """§5.4 and kn-8656d497: a worker's prompt carries first lines, never bodies."""
    finding = a_finding(root_cause="The dispatch brief   names no entry\n  point at all.")

    first = findings.knowledge_text(finding).splitlines()[0]

    assert first == "The dispatch brief names no entry point at all."
    assert findings.knowledge_text(finding).splitlines()[1] == ""


def test_the_review_event_renders_with_a_label(reviewable, store):
    """kn-3f133363: an unlabelled kind renders as a bare string beside a JSON blob."""
    io, analyst = reviewable
    ops.review_findings(io["id"], accept=["first-turn-reads"],
                        reject={"stale-code-map": "the map is about to be deleted"})

    entry = next(e for e in timeline.build_timeline(
        {}, store.events_of_kind(analyst["id"], "findings_reviewed"), [],
        include_debug=True) if e["kind"] == "findings_reviewed")

    assert entry["label"] == "Findings reviewed by user"
    assert entry["detail"] == "1 accepted, 1 rejected, 1 order(s) filed"


# -- the back-link, read from the improvement order ---------------------------------------

def test_show_reports_each_filed_orders_live_status(reviewable, store):
    io, _ = reviewable
    out = ops.review_findings(io["id"], accept=["first-turn-reads"])
    filed_id = out["created"][0]["id"]

    detail = ops.show_improvement_order(io["id"])

    entry = detail["filed_orders"]["first-turn-reads"][0]
    assert entry == {"id": filed_id, "type": "work", "project": "proj_a",
                     "title": "name the entry point in every dispatch brief",
                     "status": "pending"}

    # LIVE, not the snapshot taken at filing (§5.3.1).
    store.update_work_order(filed_id, status="running")
    again = ops.show_improvement_order(io["id"])
    assert again["filed_orders"]["first-turn-reads"][0]["status"] == "running"


def test_show_reports_a_deleted_filed_order_as_deleted(reviewable, store):
    io, _ = reviewable
    out = ops.review_findings(io["id"], accept=["first-turn-reads"])
    ops.delete_work_order(out["created"][0]["id"])

    detail = ops.show_improvement_order(io["id"])

    assert detail["filed_orders"]["first-turn-reads"][0]["status"] == "deleted"
    assert detail["findings"] == 2  # the existing keys and counts are untouched
    assert detail["by_decision"]["accepted"] == 1


# -- the CLI ----------------------------------------------------------------------------

def test_cli_io_review_accepts_and_rejects_in_one_call(reviewable, store, capsys):
    io, _ = reviewable

    assert cli.main(["io", "review", io["id"],
                     "--accept", "first-turn-reads",
                     "--reject", "stale-code-map",
                     "--feedback", "the map is about to be deleted outright",
                     "--json"]) == 0

    out = _json.loads(capsys.readouterr().out)
    assert out["status"] == "completed"
    assert len(out["created"]) == 1
    assert out["rejected"] == ["stale-code-map"]
    assert store.get_feature_order(io["id"])["status"] == "completed"


def test_cli_io_review_refuses_a_rejection_with_no_feedback(reviewable, capsys):
    io, _ = reviewable

    assert cli.main(["io", "review", io["id"], "--reject", "stale-code-map"]) == 1

    assert "--feedback" in capsys.readouterr().err


def test_cli_io_show_lists_each_accepted_findings_filed_orders(reviewable, capsys):
    io, _ = reviewable
    out = ops.review_findings(io["id"], accept=["first-turn-reads"])
    capsys.readouterr()

    assert cli.main(["io", "show", io["id"]]) == 0

    printed = capsys.readouterr().out
    assert "[first-turn-reads] accepted" in printed
    assert out["created"][0]["id"] in printed
    assert "pending" in printed
    assert "[stale-code-map] pending" in printed
