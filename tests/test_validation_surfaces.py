"""What the user is shown of a validation: rounds everywhere, seats only on demand"""

from __future__ import annotations

import ast
import json
import os
import subprocess
from pathlib import Path

import pytest

from jarvis import cli, invariants, ops
from jarvis.project_store import ProjectStore
from jarvis.testing import make_git_project


# -- fixtures: rounds and opinions, written straight into the store


def _rounds(store: ProjectStore, wo_id=None, fo_id=None) -> list[dict]:
    """Two settled rounds on one unit: a rejection, then a pass"""
    subject = {"wo_id": wo_id} if wo_id else {"fo_id": fo_id}
    first = store.open_validation_round(**subject, fingerprint="aaaa1111",
                                        summary="added the exporter",
                                        evidence="ran pytest -q")
    store.close_validation_round(first["id"], "rejected",
                                 "no test touches the new branch")
    store.record_validation_opinion(first["id"], "tester", reply=TESTER_REPLY,
                                    verdict="reject", model="sonnet", latency_ms=1200)
    store.record_validation_opinion(first["id"], "security", reply="nothing exposed",
                                    verdict="pass", model="sonnet", latency_ms=900)
    second = store.open_validation_round(**subject, fingerprint="bbbb2222",
                                         summary="added the exporter",
                                         evidence="ran pytest -q, plus a new case")
    store.close_validation_round(second["id"], "passed", "the new case covers it")
    store.record_validation_opinion(second["id"], "tester", reply="covered now",
                                    verdict="pass", model="sonnet", latency_ms=1100)
    return [first, second]


TESTER_REPLY = "you claim tests were added and no file under tests/ appears in the diff"


def _validated_wo(project) -> str:
    store = ProjectStore(project)
    try:
        wo = store.create_work_order("ship the exporter")
        _rounds(store, wo_id=wo["id"])
        return wo["id"]
    finally:
        store.close()


def _validated_fo(project) -> str:
    store = ProjectStore(project)
    try:
        fo = store.create_feature_order("CSV export", description="the whole ask")
        _rounds(store, fo_id=fo["id"])
        return fo["id"]
    finally:
        store.close()


def _out(capsys, argv: list[str]) -> str:
    assert cli.main(argv) == 0, argv
    return capsys.readouterr().out


# -- rounds on the default documents


def test_wo_show_lists_its_rounds_oldest_first(jarvis_home, catalog_file, project,
                                               capsys):
    ops.start_os(str(catalog_file), foreground=True)
    wo_id = _validated_wo(project)

    out = _out(capsys, ["wo", "show", wo_id])

    assert "round 1" in out and "round 2" in out
    assert out.index("round 1") < out.index("round 2")
    assert "aaaa1111" in out and "bbbb2222" in out
    assert "rejected" in out and "passed" in out
    assert "no test touches the new branch" in out


def test_fo_show_lists_its_rounds_oldest_first_and_names_its_manager(
        jarvis_home, catalog_file, project, capsys):
    ops.start_os(str(catalog_file), foreground=True)
    fo_id = _validated_fo(project)
    store = ProjectStore(project)
    try:
        mgr = store.create_work_order("own the follow-through", kind="manager",
                                      parent_id=fo_id)
    finally:
        store.close()

    out = _out(capsys, ["fo", "show", fo_id])

    assert out.index("round 1") < out.index("round 2")
    assert "aaaa1111" in out and "bbbb2222" in out
    assert "the new case covers it" in out
    assert f"manager: {mgr['id']}" in out


def test_wo_show_json_adds_the_rounds_and_changes_nothing_else(
        jarvis_home, catalog_file, project, capsys):
    """Additive means additive: other tooling reads this document"""
    ops.start_os(str(catalog_file), foreground=True)
    store = ProjectStore(project)
    try:
        wo = store.create_work_order("ship the exporter")
    finally:
        store.close()

    before = json.loads(_out(capsys, ["--json", "wo", "show", wo["id"]]))
    assert before["validation_rounds"] == []   # present even when empty

    store = ProjectStore(project)
    try:
        _rounds(store, wo_id=wo["id"])
    finally:
        store.close()
    after = json.loads(_out(capsys, ["--json", "wo", "show", wo["id"]]))

    assert set(after) == set(before)
    for key in before:
        # `time_in_state` is a present-tense reading (`ops.state_durations`)
        if key in ("validation_rounds", "time_in_state"):
            continue
        assert type(after[key]) is type(before[key]), key
        assert after[key] == before[key], key

    assert [r["round"] for r in after["validation_rounds"]] == [1, 2]
    assert after["validation_rounds"][0]["fingerprint"] == "aaaa1111"
    assert after["validation_rounds"][0]["outcome"] == "rejected"
    assert after["validation_rounds"][1]["outcome"] == "passed"


def test_a_work_order_with_no_rounds_reads_exactly_as_it_did(
        jarvis_home, catalog_file, project, capsys):
    """The control for every test above: validation ships disabled"""
    ops.start_os(str(catalog_file), foreground=True)
    store = ProjectStore(project)
    try:
        wo = store.create_work_order("ship the exporter")
    finally:
        store.close()

    out = _out(capsys, ["wo", "show", wo["id"]])

    assert "validation_rounds" not in out
    assert "round 1" not in out


# -- the separation: deliberation is on demand and nowhere else


def test_the_seats_are_absent_by_default_and_present_on_demand(
        jarvis_home, catalog_file, project, capsys):
    """THE PAIRING THAT MAKES THE ABSENCE MEAN ANYTHING"""
    ops.start_os(str(catalog_file), foreground=True)
    wo_id = _validated_wo(project)
    fo_id = _validated_fo(project)

    default_wo = _out(capsys, ["wo", "show", wo_id])
    default_fo = _out(capsys, ["fo", "show", fo_id])
    for doc in (default_wo, default_fo):
        assert TESTER_REPLY not in doc
        assert "tester" not in doc
        assert "security" not in doc
    # ...and the rounds themselves ARE there
    assert "rejected" in default_wo and "rejected" in default_fo

    for unit in (wo_id, fo_id):
        deep = _out(capsys, ["validation", "show", unit])
        assert TESTER_REPLY in deep
        assert "tester" in deep and "security" in deep
        assert "1200ms" in deep
        assert "abstained" not in deep      # nothing here abstained; the column is real


def test_the_timeline_of_a_rejection_carries_no_seat(
        jarvis_home, catalog_file, project, capsys):
    """A rejection reaches the timeline as an outcome and a reason — the ask the worker has to answer"""
    ops.start_os(str(catalog_file), foreground=True)
    store = ProjectStore(project)
    try:
        wo = store.create_work_order("ship the exporter")
        rounds = _rounds(store, wo_id=wo["id"])
        store.add_event(wo["id"], "validation_rejected",
                        {"round": 1, "round_id": rounds[0]["id"],
                         "reason": "no test touches the new branch"})
    finally:
        store.close()

    timeline = _out(capsys, ["wo", "show", wo["id"]])
    assert "Validation rejected" in timeline
    assert "no test touches the new branch" in timeline
    assert "tester" not in timeline and "security" not in timeline

    deep = _out(capsys, ["validation", "show", wo["id"]])
    assert "tester" in deep and "security" in deep


def test_validation_show_says_so_plainly_when_nothing_ever_ran(
        jarvis_home, catalog_file, project, capsys):
    """`neo show --panel`'s behaviour"""
    ops.start_os(str(catalog_file), foreground=True)
    store = ProjectStore(project)
    try:
        wo = store.create_work_order("ship the exporter")
        fo = store.create_feature_order("CSV export", description="the whole ask")
    finally:
        store.close()

    for unit, word in ((wo["id"], "work order"), (fo["id"], "feature order")):
        out = _out(capsys, ["validation", "show", unit])
        assert f"no validation has run on this {word}" in out
        assert "round 1" not in out


def test_validation_show_finds_a_feature_order_by_its_id_alone(
        jarvis_home, catalog_file, project, capsys):
    """One command, both units: the id says which, so the caller never has to"""
    ops.start_os(str(catalog_file), foreground=True)
    fo_id = _validated_fo(project)

    out = _out(capsys, ["validation", "show", fo_id])
    assert "feature order" in out
    assert "aaaa1111" in out


# -- envelopes


def test_doctor_reports_a_lost_envelope_and_ignores_a_delivered_one(
        jarvis_home, catalog_file, project, capsys):
    """An undeliverable envelope is feedback that reached NOBODY"""
    ops.start_os(str(catalog_file), foreground=True)
    store = ProjectStore(project)
    try:
        lost = store.create_work_order("the cancelled one")
        fine = store.create_work_order("the delivered one")
        lost_env = store.post_envelope(from_role="reviewer", to_role="implementor",
                                       kind="review_feedback", subject_wo_id=lost["id"],
                                       payload={"reason": "no tests"})
        good_env = store.post_envelope(from_role="reviewer", to_role="implementor",
                                       kind="review_feedback", subject_wo_id=fine["id"],
                                       payload={"reason": "no tests"})
        store.mark_envelope(lost_env, "undeliverable",
                            note="no work order fills role implementor")
        store.mark_envelope(good_env, "delivered", delivered_wo_id=fine["id"])
    finally:
        store.close()

    assert cli.main(["doctor"]) == 1
    out = capsys.readouterr().out

    assert "INV-ENVELOPE-LOST" in out
    assert lost["id"] in out
    assert "reached nobody" in out
    assert fine["id"] not in out


def test_a_lost_envelope_goes_quiet_once_its_subject_is_settled(
        jarvis_home, catalog_file, project, capsys):
    """The bound that keeps a clean bill of health meaningful"""
    ops.start_os(str(catalog_file), foreground=True)
    store = ProjectStore(project)
    try:
        wo = store.create_work_order("the cancelled one")
        env = store.post_envelope(from_role="reviewer", to_role="implementor",
                                  kind="review_feedback", subject_wo_id=wo["id"])
        store.mark_envelope(env, "undeliverable", note="nobody fills role implementor")
    finally:
        store.close()

    assert cli.main(["doctor"]) == 1        # open: it is reported
    assert "INV-ENVELOPE-LOST" in capsys.readouterr().out

    store = ProjectStore(project)
    try:
        store.set_status(wo["id"], "cancelled")
    finally:
        store.close()

    assert cli.main(["doctor"]) == 0        # settled: it is not
    out = capsys.readouterr().out
    assert "INV-ENVELOPE-LOST" not in out
    assert "all OS invariants hold" in out

    # ...and it is still on the record, which is what makes going quiet acceptable
    deep = _out(capsys, ["validation", "show", wo["id"]])
    assert "undeliverable" in deep
    assert "nobody fills role implementor" in deep


def test_validation_show_carries_the_delivered_envelopes_too(
        jarvis_home, catalog_file, project, capsys):
    """Delivered envelopes are routine: they belong in the on-demand view and in no default listing"""
    ops.start_os(str(catalog_file), foreground=True)
    store = ProjectStore(project)
    try:
        wo = store.create_work_order("ship the exporter")
        env = store.post_envelope(from_role="reviewer", to_role="implementor",
                                  kind="review_feedback", subject_wo_id=wo["id"])
        store.mark_envelope(env, "delivered", delivered_wo_id=wo["id"])
    finally:
        store.close()

    assert "review_feedback" not in _out(capsys, ["wo", "show", wo["id"]])

    deep = _out(capsys, ["validation", "show", wo["id"]])
    assert "review_feedback" in deep
    assert "delivered" in deep


def _lost_manager_envelope(project) -> tuple[str, str, str]:
    """fo-ac00376e's shape: a settled manager"""
    store = ProjectStore(project)
    try:
        fo = store.create_feature_order(title="CSV export", description="export things")
        store.set_feature_status(fo["id"], "executing")
        manager = store.create_manager_order(fo["id"])
        store.set_status(manager["id"], "completed")
        child = store.create_work_order(title="the live one", description="do it",
                                        origin="jarvis", kind="worker",
                                        parent_id=fo["id"])
        store.set_status(child["id"], "running")
        env = store.post_envelope(from_role="implementor", to_role="manager",
                                  kind="deferral_request", subject_wo_id=child["id"],
                                  payload={"title": "split the writer",
                                           "why": "out of scope"})
        store.mark_envelope(env, "undeliverable",
                            note=f"the manager work order {manager['id']} is completed, "
                                 f"so nothing can act on this")
        return fo["id"], manager["id"], child["id"]
    finally:
        store.close()


def test_the_lost_manager_envelope_leads_with_the_settled_manager(jarvis_home,
                                                                  catalog_file, project):
    """The user read wo-3db50904 as the completed order"""
    from jarvis import invariants

    ops.start_os(str(catalog_file), foreground=True)
    fo_id, manager_id, subject_id = _lost_manager_envelope(project)
    store = ProjectStore(project)
    try:
        found = [v for v in invariants.check_project(store)
                 if v.invariant == "INV-ENVELOPE-LOST"]
    finally:
        store.close()

    assert len(found) == 1
    detail = found[0].detail
    assert detail.index(manager_id) < detail.index(subject_id)
    assert detail.startswith(f"the manager work order {manager_id} is completed under "
                             f"feature {fo_id}")
    assert "reached nobody" in detail
    assert f"{subject_id} is held until the feature is live again" in detail
    assert found[0].context["manager_wo_id"] == manager_id
    assert found[0].context["manager_status"] == "completed"


def test_a_lost_implementor_envelope_keeps_the_old_sentence(jarvis_home, catalog_file,
                                                            project):
    """The control: only the manager case had a second order in it to confuse"""
    from jarvis import invariants

    ops.start_os(str(catalog_file), foreground=True)
    store = ProjectStore(project)
    try:
        wo = store.create_work_order("ship the exporter")
        store.set_status(wo["id"], "running")
        env = store.post_envelope(from_role="reviewer", to_role="implementor",
                                  kind="review_feedback", subject_wo_id=wo["id"],
                                  payload={"round": 1, "outcome": "rejected",
                                           "reason": "no tests"})
        store.mark_envelope(env, "undeliverable",
                            note="no work order fills role implementor")
        found = [v for v in invariants.check_project(store)
                 if v.invariant == "INV-ENVELOPE-LOST"]
    finally:
        store.close()

    assert len(found) == 1
    assert found[0].detail == (
        f"envelope {env} (review_feedback to role implementor) about {wo['id']} reached "
        f"nobody: no work order fills role implementor")
    assert found[0].context.get("manager_wo_id") is None


# -- the review control an escalated round owes


def escalated(project, title: str = "the panel gave up on it",
              cause: str = ops.ESCALATION_ROUNDS_SPENT) -> dict:
    """A work order in the live shape of wo-659be188: `needs_review`"""
    store = ProjectStore(project)
    try:
        wo = store.create_work_order(title)
        round_row = store.open_validation_round(wo_id=wo["id"], fingerprint="cccc3333",
                                               summary="ship it", evidence="ran pytest")
        ops.escalate_validation_round(store, wo, round_row["id"], round_row["round"],
                                      "two rounds running asked for the same test",
                                      cause=cause)
        return store.get_work_order(wo["id"])
    finally:
        store.close()


def test_review_state_owes_a_decision_on_an_escalated_round(jarvis_home, catalog_file,
                                                            project):
    wo = escalated(project)
    store = ProjectStore(project)
    try:
        owed = ops.review_state(store, wo)
    finally:
        store.close()

    assert owed["pending"] == 0
    assert owed["escalated"] is True
    assert owed["round"] == 1
    assert owed["placement"] == "validation"          # no assumption to host it
    assert "over the panel's objection" in owed["accept"]
    assert "worker" in owed["reject"]
    assert "whole" in owed["scope"]
    assert "flagged" in owed["strands"]


def test_the_review_control_names_the_round_that_gave_up_not_the_row_after_it(
        jarvis_home, catalog_file, project):
    """§A of docs/superpowers/specs/2026-10-14-a-message-must-not-drop-an-owed-review.md: the boolean and..."""
    store = ProjectStore(project)
    try:
        wo = store.create_work_order("the panel gave up on round 4")
        for n in (1, 2, 3):
            earlier = store.open_validation_round(wo_id=wo["id"], fingerprint=f"r{n}")
            store.close_validation_round(earlier["id"], "rejected", "")
        gave_up = store.open_validation_round(wo_id=wo["id"], fingerprint="r4")
        ops.escalate_validation_round(store, store.get_work_order(wo["id"]),
                                      gave_up["id"], gave_up["round"],
                                      "four rounds asked for the same test",
                                      cause=ops.ESCALATION_ROUNDS_SPENT)
        after = store.open_validation_round(wo_id=wo["id"], fingerprint="r5")
        assert after["round"] == 5                      # the row that judged nothing

        owed = ops.review_state(store, store.get_work_order(wo["id"]))
    finally:
        store.close()

    assert owed["escalated"] is True
    assert owed["round"] == 4
    # `escalation_cause` is keyed on the round's id
    assert owed["cause"] == ops.ESCALATION_ROUNDS_SPENT
    assert owed["family"] == "handover"
    assert "round 4 was its last" in owed["why"]


def test_review_state_places_the_one_control_in_the_assumptions_block(
        jarvis_home, catalog_file, project):
    """Both true at once is reachable (issue 212)"""
    ops.start_os(str(catalog_file), foreground=True)
    wo = escalated(project)
    ops.assume(wo["id"], "assumed the API is v2")
    store = ProjectStore(project)
    try:
        owed = ops.review_state(store, store.get_work_order(wo["id"]))
    finally:
        store.close()

    assert owed["pending"] == 1
    assert owed["escalated"] is True
    assert owed["round"] == 1
    assert owed["placement"] == "assumptions"
    assert "assumption" in owed["accept"] and "assumption" in owed["reject"]


def test_wo_show_prints_the_owed_decision_and_json_keeps_the_counts(
        jarvis_home, catalog_file, project, capsys):
    ops.start_os(str(catalog_file), foreground=True)
    wo = escalated(project)

    out = _out(capsys, ["wo", "show", wo["id"]])
    assert "over the panel's objection" in out
    assert "round 1" in out

    doc = json.loads(_out(capsys, ["--json", "wo", "show", wo["id"]]))
    assert doc["review"]["placement"] == "validation"
    assert doc["review"]["pending"] == 0
    assert doc["review"]["round"] == 1


def test_no_decision_owed_means_no_key_at_all(jarvis_home, catalog_file, project,
                                              capsys):
    """§4's never-always rule: `None` means the key is ABSENT, like `auto_merge`"""
    ops.start_os(str(catalog_file), foreground=True)
    store = ProjectStore(project)
    try:
        wo = store.create_work_order("nothing owed")
        assert ops.review_state(store, wo) is None
    finally:
        store.close()

    doc = json.loads(_out(capsys, ["--json", "wo", "show", wo["id"]]))
    assert "review" not in doc


# -- a pending assumption says WHO it is waiting on, and where it was raised (#1265)


def test_a_pending_assumption_names_the_setting_that_left_it_with_the_user(
        jarvis_home, catalog_file, project):
    """The whole defect: auto-review off writes no event, so the card said nothing"""
    ops.start_os(str(catalog_file), foreground=True)
    store = ProjectStore(project)
    try:
        wo = store.create_work_order("nobody judged this")
        ops.assume(wo["id"], "assumed the API is v2")
        row = ops.assumptions_with_rulings(store, wo["id"])[0]
    finally:
        store.close()

    # The shipped default, and the one the reporter was on
    assert row["os_ruling"] is None
    line = ops.assumption_routing_line(row)
    assert "`validation.enabled` is off" in line
    assert "nobody judged it before you" in line


def test_a_row_the_os_ruled_on_keeps_its_ruling_and_gains_no_routing_line(
        jarvis_home, catalog_file, project):
    """Never both: `assumption_ruling_line` already says who looked"""
    ops.start_os(str(catalog_file), foreground=True)
    store = ProjectStore(project)
    try:
        wo = store.create_work_order("Neo escalated this one")
        ops.assume(wo["id"], "assumed the API is v2")
        row = store.all_assumptions(wo["id"])[0]
        store.add_event(wo["id"], "autoreview_escalated",
                        {"assumption_id": row["id"], "n": 1, "reason": "too big a call",
                         "neo_question_id": 7})
        ruled = ops.assumptions_with_rulings(store, wo["id"])[0]
    finally:
        store.close()

    assert "Neo escalated (question 7)" in ops.assumption_ruling_line(ruled)
    assert ops.assumption_routing_line(ruled) == ""


def test_a_settled_assumption_has_no_routing_line(jarvis_home, catalog_file, project):
    """It is not with anybody any more"""
    ops.start_os(str(catalog_file), foreground=True)
    store = ProjectStore(project)
    try:
        wo = store.create_work_order("already decided")
        ops.assume(wo["id"], "assumed the API is v2")
        row = store.all_assumptions(wo["id"])[0]
        store.review_assumption(row["id"], "accepted", decided_by="user", reason="fine")
        settled = ops.assumptions_with_rulings(store, wo["id"])[0]
    finally:
        store.close()

    assert ops.assumption_routing_line(settled) == ""


def test_an_assumption_points_at_the_turn_that_raised_it(jarvis_home, catalog_file,
                                                         project):
    """`jarvis wo assume` runs inside a turn, so the turn's reply is the FIRST one after it"""
    ops.start_os(str(catalog_file), foreground=True)
    store = ProjectStore(project)
    try:
        wo = store.create_work_order("two turns, one assumption")
        earlier = store.record_agent_reply(wo["id"], "first turn, nothing assumed yet")
        ops.assume(wo["id"], "assumed the API is v2")
        raised_in = store.record_agent_reply(wo["id"], "recorded assumption #1 — why")
        store.record_agent_reply(wo["id"], "a later turn, unrelated")
        row = ops.assumptions_with_rulings(store, wo["id"])[0]
    finally:
        store.close()

    assert row["source_ref"] == {"kind": "message", "id": raised_in,
                                 "label": "the turn that raised it"}
    assert row["source_ref"]["id"] != earlier


def test_an_assumption_no_reply_follows_points_at_nothing(jarvis_home, catalog_file,
                                                          project):
    """A link that resolves to nothing is worse than no link"""
    ops.start_os(str(catalog_file), foreground=True)
    store = ProjectStore(project)
    try:
        wo = store.create_work_order("the turn never ended")
        store.record_agent_reply(wo["id"], "a turn that preceded it")
        ops.assume(wo["id"], "assumed the API is v2")
        row = ops.assumptions_with_rulings(store, wo["id"])[0]
    finally:
        store.close()

    assert row["source_ref"] is None


def test_wo_show_carries_the_routing_line_so_the_cli_and_the_page_agree(
        jarvis_home, catalog_file, project, capsys):
    """§4 of the issue: one renderer, both surfaces"""
    ops.start_os(str(catalog_file), foreground=True)
    store = ProjectStore(project)
    try:
        wo = store.create_work_order("nobody judged this either")
    finally:
        store.close()
    ops.assume(wo["id"], "assumed the API is v2")

    out = _out(capsys, ["wo", "show", wo["id"]])
    assert "`validation.enabled` is off" in out
    assert "nobody judged it before you" in out


# -- an escalated round says WHY, and what to run next

#: Every cause a call site writes
CAUSES = [c for c in ops.ESCALATION_FAMILY if c != ops.ESCALATION_CAUSE_UNRECORDED]


def _git(cwd: Path, *args: str) -> str:
    env = {"HOME": str(cwd), "PATH": os.environ.get("PATH", ""),
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, env=env,
                          capture_output=True, text=True).stdout


@pytest.fixture()
def git_project(tmp_path, claude_json) -> Path:
    """A real repository whose `origin` matches the pull request the harness opens"""
    proj = make_git_project(tmp_path, "proj_a")
    _git(proj, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-qm", "base")
    _git(proj, "remote", "add", "origin", "https://github.com/acme/proj.git")
    claude_json(proj)
    return proj


def _payload(store, wo_id: str) -> dict:
    events = store.events_of_kind(wo_id, "validation_escalated")
    assert events, "nothing escalated"
    return json.loads(events[-1]["payload"])


@pytest.mark.parametrize("cause", CAUSES)
def test_escalation_records_its_cause_in_the_event_payload(jarvis_home, catalog_file,
                                                           project, cause):
    """Spec §2: the one home writes the cause, and §1's one dict gives every cause a family"""
    wo = escalated(project, cause=cause)
    store = ProjectStore(project)
    try:
        payload = _payload(store, wo["id"])
        state = ops.review_state(store, wo)
    finally:
        store.close()

    assert payload["cause"] == cause
    assert ops.ESCALATION_FAMILY[cause] in ("handover", "not_reviewed", "unjudged")
    assert state["cause"] == cause
    assert state["family"] == ops.ESCALATION_FAMILY[cause]
    assert state["why"] and state["next"]


#: Phrases that DENY anyone judged the work
NOBODY_JUDGED = ("nobody judged", "no seat judged", "nothing new to judge",
                 "could not be reached", "reached no verdict")

#: Phrases that ASSERT a panel opinion was given
PANEL_OPINION = ("the panel's asks", "the panel's objection", "the panel's verdict")


@pytest.mark.parametrize("cause", CAUSES)
def test_no_escalation_claims_a_rejection_while_denying_anyone_judged(
        jarvis_home, catalog_file, project, cause):
    """THE ROUND-1 REJECTION"""
    wo = escalated(project, cause=cause)
    store = ProjectStore(project)
    try:
        state = ops.review_state(store, wo)
    finally:
        store.close()

    verb, why = state["verb"].lower(), state["why"].lower()
    if "rejected" in verb:
        assert not [p for p in NOBODY_JUDGED if p in why], (
            f"{cause}: verb claims a rejection, why denies anyone judged")
    else:
        assert state["family"] in ("not_reviewed", "unjudged"), cause
        assert "rejected" not in verb, cause

    sentences = [state["accept"], state["reject"], *state["next"]]
    if state["family"] == "handover":
        assert [s for s in sentences if "the panel's asks" in s], cause
        assert [s for s in sentences if "the panel's objection" in s], cause
        return
    for s in sentences:
        low = s.lower()
        assert not [p for p in PANEL_OPINION if p in low], (
            f"{cause}: {s!r} cites a panel opinion nobody gave")
        if "panel verdict" in low:
            assert ("without a panel verdict" in low or "no panel verdict" in low), (
                f"{cause}: {s!r} asserts a panel verdict as given")


def test_an_unrecorded_escalation_cites_no_panel_opinion():
    """`unrecorded` is a legacy event with no cause"""
    for line in ops.escalation_next({"id": "wo-x"}, "unrecorded"):
        low = line.lower()
        assert not [p for p in PANEL_OPINION if p in low], line
        if "panel verdict" in low:
            assert "without a panel verdict" in low or "no panel verdict" in low, line


def test_every_escalation_call_site_names_its_cause():
    """Spec §2 and rejected alternative 6: KEYWORD-ONLY and NO DEFAULT"""
    import inspect

    from jarvis import daemon as daemon_mod

    cause = inspect.signature(ops.escalate_validation_round).parameters["cause"]
    assert cause.kind is inspect.Parameter.KEYWORD_ONLY
    assert cause.default is inspect.Parameter.empty

    calls = []
    for module in (ops, daemon_mod):
        tree = ast.parse(Path(module.__file__).read_text())
        calls += [node for node in ast.walk(tree) if isinstance(node, ast.Call)
                  and (getattr(node.func, "attr", None) or getattr(node.func, "id", ""))
                  in ("_escalate", "escalate_validation_round")]

    assert len(calls) >= 7, "a call site went missing"
    for call in calls:
        named = [kw.arg for kw in call.keywords]
        assert "cause" in named, f"line {call.lineno} escalates without a cause"


def test_a_rejection_on_a_user_rework_round_at_the_ceiling_says_so(
        tmp_path, git_project, jarvis_home, fake_claude, fake_gh):
    """THE #1040 SHAPE, through the real `Daemon._validate_work_order`: the round after the user's own..."""
    from tests.test_rejudge_moved_head import (
        Panel, PUSHED, _cfg, _user_rejected, artifact, boot, judge, parked)

    fleet = boot(tmp_path, git_project, max_rounds=2)
    store, wo = parked(git_project, rounds=2)          # the counted budget is spent
    _user_rejected(store, wo)
    artifact(fake_gh, head_oid=PUSHED)
    row = ops.submit_for_validation(store, git_project,
                                    store.get_work_order(wo["id"]),
                                    declared="did the rework", cfg=_cfg(fleet))
    assert row is not None and row["uncounted_cause"] == ops.USER_REWORK_CAUSE

    judge(fleet, store, Panel("rejected"))

    fresh = store.get_work_order(wo["id"])
    assert fresh["status"] == "needs_review"
    assert _payload(store, wo["id"])["cause"] == ops.ESCALATION_USER_REWORK_CEILING
    state = ops.review_state(store, fresh)
    assert state["cause"] == ops.ESCALATION_USER_REWORK_CEILING
    assert state["family"] == "handover"
    assert "had no round left to send back" in state["why"]
    assert "gave up" not in state["why"]
    store.close()


def test_a_rejection_with_the_counted_budget_spent_says_rounds_spent(
        tmp_path, git_project, jarvis_home, fake_claude, fake_gh):
    """The ordinary arm of the same three-way choice"""
    from tests.test_rejudge_moved_head import (
        Panel, PUSHED, _cfg, artifact, boot, judge, parked)

    fleet = boot(tmp_path, git_project, max_rounds=2)
    store, wo = parked(git_project, rounds=1)
    artifact(fake_gh, head_oid=PUSHED)
    row = ops.submit_for_validation(store, git_project,
                                    store.get_work_order(wo["id"]),
                                    declared="another go", cfg=_cfg(fleet))
    assert row is not None and not row["uncounted"], "an ordinary, counted round"

    judge(fleet, store, Panel("rejected"))

    fresh = store.get_work_order(wo["id"])
    assert fresh["status"] == "needs_review"
    assert _payload(store, wo["id"])["cause"] == ops.ESCALATION_ROUNDS_SPENT
    assert ops.review_state(store, fresh)["family"] == "handover"
    store.close()


def test_a_rejected_rebind_with_the_rebind_budget_spent_says_rebinds_spent(
        tmp_path, git_project, jarvis_home, fake_claude, fake_gh):
    """The third arm, and the one the round NUMBER cannot name: a rebind sits past `max_rounds` and is..."""
    from tests.test_rejudge_moved_head import (
        AGAIN, Panel, PUSHED, _cfg, _moved, artifact, boot, judge, parked)

    fleet = boot(tmp_path, git_project, max_rounds=3)
    store, wo = parked(git_project, rounds=3)
    artifact(fake_gh, head_oid=PUSHED)
    ops.rejudge_moved_head(store, git_project, wo, project="proj_a", cfg=_cfg(fleet),
                           decision=_moved(PUSHED), rebind=True)
    first = store.latest_validation_round(wo_id=wo["id"])
    store.set_validation_head(first["id"], PUSHED)
    store.close_validation_round(first["id"], "passed", "")
    store.set_status(wo["id"], "waiting_pr_merge")

    artifact(fake_gh, head_oid=AGAIN)
    ops.rejudge_moved_head(store, git_project, store.get_work_order(wo["id"]),
                           project="proj_a", cfg=_cfg(fleet),
                           decision=_moved(AGAIN), rebind=True)
    judge(fleet, store, Panel("rejected"))

    fresh = store.get_work_order(wo["id"])
    assert fresh["status"] == "needs_review"
    assert _payload(store, wo["id"])["cause"] == ops.ESCALATION_REBINDS_SPENT
    state = ops.review_state(store, fresh)
    assert state["family"] == "handover"
    assert "the OS asked for itself" in state["why"]
    store.close()


@pytest.mark.parametrize("cause", [ops.ESCALATION_ROUNDS_SPENT,
                                   ops.ESCALATION_NO_VERDICT])
def test_a_no_pr_escalated_order_is_never_pointed_at_validation_force(
        jarvis_home, catalog_file, project, capsys, cause):
    """THE REGRESSION"""
    ops.start_os(str(catalog_file), foreground=True)
    wo = escalated(project, cause=cause)

    human = _out(capsys, ["wo", "show", wo["id"]])
    raw = _out(capsys, ["--json", "wo", "show", wo["id"]])
    shown = _out(capsys, ["validation", "show", wo["id"]])

    for out in (human, raw, shown):
        assert "validation force" not in out
    assert f'jarvis wo review {wo["id"]} --reject' in human
    assert f'jarvis wo review {wo["id"]} --reject' in shown
    assert any("--reject" in line for line in json.loads(raw)["review"]["next"])


@pytest.mark.parametrize("cause", CAUSES)
def test_the_give_up_notification_names_the_family_and_never_gave_up(
        jarvis_home, catalog_file, project, cause):
    """One vocabulary on every surface — `ops.ESCALATION_VERB` (spec §3)"""
    wo = escalated(project, cause=cause)
    store = ProjectStore(project)
    try:
        titles = [row["title"] for row in store.unrouted_notifications()
                  if row["source"] == "validation"]
    finally:
        store.close()

    verb = ops.ESCALATION_VERB[ops.ESCALATION_FAMILY[cause]]
    assert titles == [ops.ESCALATION_TITLE.format(unit=wo["id"], n=1, verb=verb)]
    assert "gave up" not in titles[0]


def test_a_legacy_escalated_event_with_no_cause_field_reads_as_unrecorded(
        jarvis_home, catalog_file, project):
    """Spec §1: an event written before this change lacks the field"""
    wo = escalated(project)
    store = ProjectStore(project)
    try:
        event = store.events_of_kind(wo["id"], "validation_escalated")[-1]
        legacy = {k: json.loads(event["payload"])[k]
                  for k in ("round", "round_id", "reason")}
        store.conn.execute("UPDATE wo_events SET payload=? WHERE id=?",
                           (json.dumps(legacy), event["id"]))
        store.conn.commit()
        state = ops.review_state(store, wo)
    finally:
        store.close()

    assert state["cause"] == ops.ESCALATION_CAUSE_UNRECORDED
    assert state["family"] == "unrecorded"
    assert "before the OS recorded which condition fired" in state["why"]
    assert not [line for line in state["next"] if "validation force" in line]


@pytest.mark.parametrize("cause", CAUSES)
def test_validation_stuck_blocker_is_unchanged_for_every_cause(jarvis_home, catalog_file,
                                                               project, cause):
    """Spec §6: one attention sentence for every cause, byte for byte"""
    ops.start_os(str(catalog_file), foreground=True)
    wo = escalated(project, cause=cause)
    store = ProjectStore(project)
    try:
        assert (store.get_work_order(wo["id"])["attention_reason"]
                == invariants.VALIDATION_STUCK_BLOCKER)
    finally:
        store.close()

    ops.ack_attention(wo["id"])
    store = ProjectStore(project)
    try:
        assert not store.get_work_order(wo["id"])["needs_attention"]
    finally:
        store.close()


@pytest.mark.parametrize("cause", [ops.ESCALATION_ROUNDS_SPENT,
                                   ops.ESCALATION_NO_VERDICT])
@pytest.mark.parametrize("pr_url", ["", "https://github.com/acme/proj/pull/7"])
def test_validation_force_is_offered_only_when_the_refusal_would_allow_it(
        jarvis_home, catalog_file, project, cause, pr_url):
    """Spec §4's one-direction pin: the two predicates may differ in strictness, and only this way round (#1040)"""
    ops.start_os(str(catalog_file), foreground=True)
    wo = escalated(project, cause=cause)
    store = ProjectStore(project)
    try:
        if pr_url:
            store.update_work_order(wo["id"], pr_url=pr_url)
        fresh = store.get_work_order(wo["id"])
        state = ops.review_state(store, fresh)
        offered = [line for line in state["next"] if "validation force" in line]
        refusal = ops.force_validation_refusal(
            store, fresh, project="proj_a", cfg=ops.validation_config("proj_a"))
    finally:
        store.close()

    if offered:
        assert refusal is None or "carries no pull request" not in refusal
    assert bool(offered) == bool(pr_url and state["family"] == "unjudged")


def test_validation_show_prints_the_owed_decision_once(jarvis_home, catalog_file,
                                                       project, capsys):
    """Spec §5: after the rounds, not per round"""
    ops.start_os(str(catalog_file), foreground=True)
    wo = escalated(project)

    out = _out(capsys, ["validation", "show", wo["id"]])

    assert out.count("owed:") == 1
    assert out.count("why:") == 1
    assert out.index("round 1") < out.index("owed:")
