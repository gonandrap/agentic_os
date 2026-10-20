"""`jarvis validation force`: a person opens a round, and no worker is pretended (docs/superpowers/specs/2026-09-15-forcing-a-validation-round.md)"""

from __future__ import annotations

import json
import os
import subprocess
import time
from pathlib import Path

import pytest

from jarvis import cli, ops
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.project_store import FORCEABLE_STATUSES, ProjectStore
from jarvis.testing import make_git_project

PR = "https://github.com/acme/proj/pull/7"
#: What round 1 recorded on every work order judged before `head_sha` shipped
UNRECORDED = ""
LIVE_HEAD = "c0ffee11c0ffee22c0ffee33c0ffee44c0ffee55"
GREEN = [{"__typename": "CheckRun", "name": "unit", "status": "COMPLETED",
          "conclusion": "SUCCESS"}]


def _git(cwd: Path, *args: str) -> str:
    env = {"HOME": str(cwd), "PATH": os.environ.get("PATH", ""),
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, env=env,
                          capture_output=True, text=True).stdout


def write_catalog(tmp_path: Path, project: Path, **validation) -> Path:
    """A catalog ON DISK with the panel on"""
    data = {
        "os": {
            "defaults": {"model": "sonnet", "max_in_flight": 50},
            "notifications": {"sinks": ["log"]},
            "validation": {"enabled": True, **validation},
        },
        "projects": [{"name": "proj_a", "path": str(project),
                      "description": "test project"}],
    }
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(data))
    return path


@pytest.fixture()
def project(tmp_path, claude_json) -> Path:
    """A real repository whose `origin` matches `PR` — the collector reads both"""
    proj = make_git_project(tmp_path, "proj_a")
    _git(proj, "symbolic-ref", "HEAD", "refs/heads/main")
    _git(proj, "add", "-A")
    _git(proj, "commit", "-qm", "base")
    _git(proj, "remote", "add", "origin", "https://github.com/acme/proj.git")
    claude_json(proj)
    return proj


@pytest.fixture()
def fleet(tmp_path, jarvis_home, fake_claude, project):
    catalog_path = write_catalog(tmp_path, project)
    ops.start_os(str(catalog_path), foreground=True)
    return Daemon(load_catalog(catalog_path))


class Panel:
    """The injected validator seam, driven by the test"""

    def __init__(self, outcome: str = "passed"):
        self.outcome = outcome
        self.rounds: list[int] = []

    def __call__(self, store, round_row, packet):
        self.rounds.append(int(round_row["round"]))
        return {"outcome": self.outcome, "reason": "",
                "seats": [{"seat": "tester", "status": "ok",
                           "verdict": "pass" if self.outcome == "passed" else "reject",
                           "model": "sonnet", "latency_ms": 3, "reply": "ok"}]}


def parked(fleet, project, *, judged: str = UNRECORDED, outcome: str = "passed",
           pr_url: str | None = PR) -> tuple[ProjectStore, dict]:
    """A work order parked in `waiting_pr_merge` with ONE settled round on `judged`"""
    store = ProjectStore(project)
    wo = ops.create_work_order("proj_a", "add feature X")
    ops.finish(wo["id"], "opened a PR", pr_url=pr_url, evidence="ran the suite")
    # `finish` opened round 1 itself (the panel is on)
    round_row = store.latest_validation_round(wo_id=wo["id"])
    assert round_row is not None and int(round_row["round"]) == 1
    store.set_validation_head(round_row["id"], judged)
    if outcome != "pending":
        store.close_validation_round(round_row["id"], outcome, "")
    store.set_status(wo["id"], "waiting_pr_merge")
    return store, store.get_work_order(wo["id"])


def judge(fleet, store, panel: Panel, timeout: float = 15.0) -> None:
    """Run whatever round is open to a verdict, off the daemon's tick thread"""
    fleet.validator = panel
    fleet.validation_tick(fleet.catalog.project("proj_a"), store)
    deadline = time.monotonic() + timeout
    while fleet.validating and time.monotonic() < deadline:
        time.sleep(0.01)
    assert not fleet.validating, "a validation round never finished"


def artifact(fake_gh, *, head_oid: str = LIVE_HEAD) -> None:
    """Register the pull request BOTH readers see — the collector and the poll"""
    fake_gh.set_pr_artifact(
        PR, title="[wo-1] add feature X", body="## Summary\nit adds feature X",
        diff="diff --git a/app.py b/app.py\n@@ -0,0 +1 @@\n+feature\n",
        files=[{"path": "app.py", "additions": 1, "deletions": 0}],
        checks=GREEN, head_oid=head_oid)
    fake_gh.set_pr(PR, "OPEN", checks=GREEN, merge_state="CLEAN", head_oid=head_oid)


# -- the bug: a round that recorded no commit gets one


def test_a_forced_round_records_the_commit_the_round_before_it_could_not(
        fleet, project, fake_gh):
    """THE WHOLE BUG, start to finish"""
    store, wo = parked(fleet, project)
    artifact(fake_gh)
    assert ProjectStore.validated_head(
        store.latest_validation_round(wo_id=wo["id"])) is None

    ops.force_validation(wo["id"], reason="round 1 predates head_sha")
    judge(fleet, store, Panel("passed"))

    rounds = store.validation_rounds(wo_id=wo["id"])
    assert [r["round"] for r in rounds] == [1, 2]
    assert rounds[0]["head_sha"] == ""        # untouched — history is not rewritten
    assert rounds[1]["head_sha"] == LIVE_HEAD
    assert ProjectStore.validated_head(rounds[1]) == LIVE_HEAD


def test_the_forced_round_is_what_finally_lets_the_pull_request_merge_itself(
        fleet, project, fake_gh, local_base):
    """The motivating outcome, and why stopping at the column would prove too little: the point is not..."""
    store, wo = parked(fleet, project)
    artifact(fake_gh)
    spec = fleet.catalog.project("proj_a")
    spec.validation.enabled = spec.validation.auto_merge = True

    fleet.poll_pull_requests(spec, store)
    assert store.list_approvals(wo["id"]) == []  # held: the panel named no commit
    assert store.get_work_order(wo["id"])["status"] == "validating"

    judge(fleet, store, Panel("passed"))
    assert store.get_work_order(wo["id"])["status"] == "waiting_pr_merge"

    fleet.poll_pull_requests(spec, store)
    assert [a["kind"] for a in store.list_approvals(wo["id"])] == ["auto_merge"]


def test_a_forced_round_re_reads_the_pull_request_rather_than_the_old_packet(
        fleet, project, fake_gh):
    """The evidence is collected FRESH"""
    store, wo = parked(fleet, project)
    artifact(fake_gh, head_oid="aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111")
    ops.force_validation(wo["id"], reason="first attempt")
    judge(fleet, store, Panel("passed"))

    artifact(fake_gh, head_oid=LIVE_HEAD)  # the worker pushed
    ops.force_validation(wo["id"], reason="it moved under us")
    judge(fleet, store, Panel("passed"))

    rounds = store.validation_rounds(wo_id=wo["id"])
    assert rounds[-1]["round"] == 3 and rounds[-1]["head_sha"] == LIVE_HEAD


# -- the repeat-submission guard does not eat it (issue #272)


def test_a_forced_round_on_an_unchanged_branch_is_judged_rather_than_discarded(
        fleet, project, fake_gh):
    """THE DEFECT"""
    artifact(fake_gh)
    store, wo = parked(fleet, project)

    ops.force_validation(wo["id"], reason="round 1 recorded no commit")
    panel = Panel("passed")
    judge(fleet, store, panel)

    rounds = store.validation_rounds(wo_id=wo["id"])
    assert rounds[0]["fingerprint"] == rounds[1]["fingerprint"], (
        "the collision this test is about never happened")
    assert panel.rounds == [2], "no seat opined on the forced round"
    assert rounds[1]["outcome"] == "passed"
    assert store.events_of_kind(wo["id"], "validation_escalated") == []
    assert store.get_work_order(wo["id"])["status"] == "waiting_pr_merge"


def test_forcing_twice_over_the_same_pull_request_is_judged_twice(fleet, project,
                                                                 fake_gh):
    """The guard compares against the IMMEDIATELY PRECEDING round"""
    artifact(fake_gh)
    store, wo = parked(fleet, project)

    ops.force_validation(wo["id"], reason="first re-judgement")
    judge(fleet, store, Panel("passed"))
    ops.force_validation(wo["id"], reason="and again under the new panel")
    second = Panel("passed")
    judge(fleet, store, second)

    assert second.rounds == [3]
    rounds = store.validation_rounds(wo_id=wo["id"])
    assert [r["outcome"] for r in rounds] == ["passed"] * 3


def test_an_ordinary_resubmission_that_changed_nothing_is_still_refused(fleet, project,
                                                                       fake_gh):
    """The negative control, and the reason the exemption is keyed on `forced_reason` rather than removed:..."""
    artifact(fake_gh)
    store, wo = parked(fleet, project, outcome="rejected")
    store.set_status(wo["id"], "running")

    ops.finish(wo["id"], "opened a PR", pr_url=PR, evidence="ran the suite")
    panel = Panel("passed")
    judge(fleet, store, panel)

    assert panel.rounds == [], "a repeat submission reached the panel"
    reason = store.validation_rounds(wo_id=wo["id"])[-1]["reason"]
    assert "identical to round 1" in reason
    assert store.get_work_order(wo["id"])["status"] == "needs_review"


# -- the record: no worker finished, and the reason survives


def test_forcing_a_round_never_claims_a_worker_reported_a_result(fleet, project,
                                                                fake_gh):
    """The defect in the route this replaces"""
    store, wo = parked(fleet, project)
    artifact(fake_gh)
    before = store.events_of_kind(wo["id"], "finished")

    ops.force_validation(wo["id"], reason="round 1 predates head_sha")
    judge(fleet, store, Panel("passed"))

    assert store.events_of_kind(wo["id"], "finished") == before
    assert len(before) == 1
    assert store.get_work_order(wo["id"])["result_summary"] == "opened a PR"


def test_the_reason_is_recoverable_afterwards_from_the_round_and_the_timeline(
        fleet, project, fake_gh):
    """"Forced, and why" has to outlive the terminal it was typed in"""
    store, wo = parked(fleet, project)
    artifact(fake_gh)

    ops.force_validation(wo["id"], reason="round 1 predates head_sha")

    forced = store.latest_validation_round(wo_id=wo["id"])
    assert forced is not None
    assert forced["forced_reason"] == "round 1 predates head_sha"
    payload = json.loads(
        store.events_of_kind(wo["id"], "validation_forced")[-1]["payload"])
    assert payload["reason"] == "round 1 predates head_sha"
    assert payload["round"] == 2 and payload["was"] == "waiting_pr_merge"


def test_the_terminal_line_for_a_round_says_it_was_forced(fleet, project, fake_gh):
    """`ops.round_line` — the ONE formatter behind `jarvis wo show`"""
    store, wo = parked(fleet, project)
    artifact(fake_gh)
    ops.force_validation(wo["id"], reason="round 1 predates head_sha")

    rounds = ops.validation_rounds(store, wo_id=wo["id"])
    assert "forced: round 1 predates head_sha" in ops.round_line(rounds[-1])
    # ...and an ordinary round says nothing: a line announcing "not forced" on every round in the fleet...
    assert "forced" not in ops.round_line(rounds[0])


def test_the_work_order_page_marks_the_forced_round_and_leaves_the_other_alone(
        fleet, project, fake_gh):
    """THE DASHBOARD RENDERS ITS OWN WORDING, and a `round_line` assertion never reaches it"""
    from fastapi.testclient import TestClient

    from jarvis.ui.app import create_app

    store, wo = parked(fleet, project)
    artifact(fake_gh)
    ops.force_validation(wo["id"], reason="round 1 predates head_sha")
    judge(fleet, store, Panel("passed"))

    page = TestClient(create_app(), follow_redirects=False).get(
        f"/wo/proj_a/{wo['id']}").text

    assert "forced by hand — round 1 predates head_sha" in page
    # THREE and no more, each one accounted for: the badge and the reason on round 2
    assert page.count("forced") == 3, page.count("forced")
    assert "Validation forced by hand — round 2" in page
    # ...and the raw event kind never reaches a reader: an unlabelled kind renders as the kind itself...
    assert "validation_forced" not in page
    # The commit the forced round bound the verdict to, on that same row
    assert f"@{LIVE_HEAD[:10]}" in page


def test_the_round_is_numbered_and_counted_like_any_other(fleet, project, fake_gh):
    """Neo, question 300"""
    store, wo = parked(fleet, project)
    artifact(fake_gh)

    ops.force_validation(wo["id"], reason="once")
    judge(fleet, store, Panel("passed"))
    ops.force_validation(wo["id"], reason="twice")
    judge(fleet, store, Panel("passed"))

    assert [r["round"] for r in store.validation_rounds(wo_id=wo["id"])] == [1, 2, 3]
    assert store.counted_validation_rounds(wo_id=wo["id"]) == 3


# -- numbering, once the count and the number stop being the same integer


def test_an_uncounted_round_does_not_collide_with_the_next_ordinary_round(
        fleet, project, fake_gh):
    """The trap the round above names"""
    store, wo = parked(fleet, project)
    artifact(fake_gh)
    rebind = store.open_validation_round(wo_id=wo["id"], fingerprint="fp2", round=2,
                                         uncounted=True)
    store.close_validation_round(rebind["id"], "passed", "")
    assert store.counted_validation_rounds(wo_id=wo["id"]) == 1
    assert store.numbered_validation_rounds(wo_id=wo["id"]) == 2
    assert store.uncounted_validation_rounds(wo_id=wo["id"]) == 1

    row = ops.submit_for_validation(
        store, project, store.get_work_order(wo["id"]), declared="ran the suite",
        cfg=fleet.catalog.project("proj_a").validation)

    assert int(row["round"]) == 3 and int(row["id"]) != int(rebind["id"])
    assert row["outcome"] == "pending"
    assert store.get_validation_round(int(rebind["id"]))["outcome"] == "passed"
    judge(fleet, store, Panel("passed"))
    assert store.get_work_order(wo["id"])["status"] != "validating"


@pytest.mark.parametrize("outcome", ["pending", "failed", "void", "passed", "rejected",
                                     "escalated"])
def test_the_two_counters_agree_on_a_database_with_no_rebind_in_it(
        fleet, project, fake_gh, outcome):
    """The migration claim: every row written before the column existed reads `uncounted=0`"""
    store, wo = parked(fleet, project, outcome="pending")
    row = store.latest_validation_round(wo_id=wo["id"])
    if outcome != "pending":
        store.close_validation_round(row["id"], outcome, "")

    assert store.numbered_validation_rounds(wo_id=wo["id"]) == \
        store.counted_validation_rounds(wo_id=wo["id"])
    assert store.uncounted_validation_rounds(wo_id=wo["id"]) == 0


def test_a_retried_submission_still_reuses_its_number(fleet, project, fake_gh):
    """The idempotency, asserted against the new formula: a submission retried while its round is still..."""
    store, wo = parked(fleet, project, outcome="pending")
    artifact(fake_gh)
    cfg = fleet.catalog.project("proj_a").validation
    open_row = store.latest_validation_round(wo_id=wo["id"])

    again = ops.submit_for_validation(store, project,
                                      store.get_work_order(wo["id"]),
                                      declared="ran the suite", cfg=cfg)

    assert int(again["id"]) == int(open_row["id"]) and int(again["round"]) == 1
    assert len(store.validation_rounds(wo_id=wo["id"])) == 1
    assert store.numbered_validation_rounds(wo_id=wo["id"]) == 0


def test_a_rejection_past_the_budget_comes_to_the_user_and_not_to_a_worker(
        fleet, project, fake_gh):
    """The stated `max_rounds` behaviour (spec §4)"""
    store, wo = parked(fleet, project)
    artifact(fake_gh)
    fleet.catalog.project("proj_a").validation.max_rounds = 1

    ops.force_validation(wo["id"], reason="round 1 predates head_sha")
    judge(fleet, store, Panel("rejected"))

    assert store.events_of_kind(wo["id"], "validation_escalated")
    assert store.events_of_kind(wo["id"], "validation_rejected") == []
    fresh = store.get_work_order(wo["id"])
    assert fresh["status"] == "needs_review" and fresh["attention_reason"]


# -- what it refuses, and each refusal bites


def test_a_blank_reason_is_refused(fleet, project, fake_gh):
    store, wo = parked(fleet, project)
    with pytest.raises(ops.OpsError, match="reason"):
        ops.force_validation(wo["id"], reason="   ")
    assert len(store.validation_rounds(wo_id=wo["id"])) == 1


def test_a_work_order_with_no_pull_request_is_refused(fleet, project, fake_gh):
    """A fresh round would read the worktree and record `''`"""
    store, wo = parked(fleet, project, pr_url=None)
    with pytest.raises(ops.OpsError, match="no pull request"):
        ops.force_validation(wo["id"], reason="try anyway")
    assert len(store.validation_rounds(wo_id=wo["id"])) == 1


@pytest.mark.parametrize("outcome", ["pending", "failed"])
def test_a_round_the_machine_still_owns_is_refused(fleet, project, fake_gh, outcome):
    """`ProjectStore.round_machine_owns`"""
    store, wo = parked(fleet, project, outcome=outcome)
    with pytest.raises(ops.OpsError, match=f"is {outcome} — the panel has not finished"):
        ops.force_validation(wo["id"], reason="impatient")
    assert len(store.validation_rounds(wo_id=wo["id"])) == 1


def out_of_order(store, *, high: str, low: str) -> dict:
    """A work order whose rounds were INSERTED in the wrong order"""
    wo = ops.create_work_order("proj_a", f"two rounds: {high} over {low}")
    second = store.open_validation_round(wo_id=wo["id"], fingerprint="fp2", round=2)
    store.close_validation_round(second["id"], high, "")
    first = store.open_validation_round(wo_id=wo["id"], fingerprint="fp1", round=1)
    store.close_validation_round(first["id"], low, "")
    assert first["id"] > second["id"], "the fixture must insert round 1 last"
    return wo


def test_the_open_round_predicate_keys_on_the_highest_round_not_the_newest_row(
        fleet, project):
    """THE RISK THE REWRITE INTRODUCED, both directions"""
    store = ProjectStore(project)
    # Round 2 is PENDING and round 1 (inserted last) is settled: the machine still owns it
    still_open = out_of_order(store, high="pending", low="passed")
    assert store.validation_round_open(still_open["id"]) is True

    # ...and the reverse: round 2 settled, round 1 (inserted last) left pending
    done = out_of_order(store, high="passed", low="pending")
    assert store.validation_round_open(done["id"]) is False


def test_forcing_reads_the_same_highest_round_the_predicate_does(fleet, project,
                                                                 fake_gh):
    """The refusal and the round it names come from ONE read of the HIGHEST round"""
    store = ProjectStore(project)
    wo = out_of_order(store, high="pending", low="passed")
    store.update_work_order(wo["id"], pr_url=PR)
    store.set_status(wo["id"], "waiting_pr_merge")
    artifact(fake_gh)

    with pytest.raises(ops.OpsError, match="round 2 .* is pending"):
        ops.force_validation(wo["id"], reason="impatient")
    assert len(store.validation_rounds(wo_id=wo["id"])) == 2


def test_a_work_order_nothing_has_ever_judged_is_owned_by_nobody(fleet, project):
    """None is the shape `latest_validation_round` returns for it"""
    assert ProjectStore.round_machine_owns(None) is False


@pytest.mark.parametrize("status", ["cancelled", "completed", "failed"])
def test_a_settled_work_order_is_refused(fleet, project, fake_gh, status):
    """Opening a round would put it back in `validating`"""
    store, wo = parked(fleet, project)
    store.set_status(wo["id"], status)
    with pytest.raises(ops.OpsError, match=status):
        ops.force_validation(wo["id"], reason="re-judge it")
    assert store.get_work_order(wo["id"])["status"] == status


@pytest.mark.parametrize("status", ["running", "dispatching", "waiting_input",
                                    "pending"])
def test_a_work_order_whose_worker_is_still_typing_is_refused(fleet, project, fake_gh,
                                                              status):
    """THE CASE AN ALLOWLIST CATCHES AND A SETTLED-ONLY BLOCKLIST DOES NOT"""
    store, wo = parked(fleet, project)
    store.set_status(wo["id"], status)
    with pytest.raises(ops.OpsError, match=f"is {status}, and a round can only be"):
        ops.force_validation(wo["id"], reason="re-judge it")
    assert len(store.validation_rounds(wo_id=wo["id"])) == 1
    assert store.get_work_order(wo["id"])["status"] == status


def test_the_allowlist_is_what_the_refusal_names_so_the_two_cannot_drift(fleet, project,
                                                                        fake_gh):
    """The message quotes `FORCEABLE_STATUSES`"""
    store, wo = parked(fleet, project)
    store.set_status(wo["id"], "running")
    with pytest.raises(ops.OpsError) as caught:
        ops.force_validation(wo["id"], reason="re-judge it")
    for allowed in FORCEABLE_STATUSES:
        assert allowed in str(caught.value)


def escalated(fleet, store, project, fake_gh) -> dict:
    """A work order in `needs_review` because THE PANEL GAVE UP on it — the real path"""
    store_, wo = parked(fleet, project, outcome="pending")
    assert store_ is not None
    artifact(fake_gh)
    judge(fleet, store, Panel("escalated"))
    fresh = store.get_work_order(wo["id"])
    assert fresh["status"] == "needs_review", fresh["status"]
    assert fresh["needs_attention"] == 1 and fresh["attention_reason"]
    return fresh


def test_a_delivered_order_awaiting_a_person_is_allowed(fleet, project, fake_gh):
    """`needs_review` is the OTHER half of the allowlist and not an afterthought: a panel escalation is..."""
    store = ProjectStore(project)
    wo = escalated(fleet, store, project, fake_gh)

    ops.force_validation(wo["id"], reason="re-judge the escalation on the live head")
    judge(fleet, store, Panel("passed"))

    rounds = store.validation_rounds(wo_id=wo["id"])
    assert [r["outcome"] for r in rounds] == ["escalated", "passed"]
    assert rounds[-1]["head_sha"] == LIVE_HEAD
    assert store.get_work_order(wo["id"])["status"] == "waiting_pr_merge"


def test_forcing_takes_down_the_flag_the_escalation_raised(fleet, project, fake_gh):
    """THE USER IS NOT LEFT LOOKING AT A QUESTION THE MACHINE HAS ANSWERED (kn-7e57d410)"""
    store = ProjectStore(project)
    wo = escalated(fleet, store, project, fake_gh)
    spec = fleet.catalog.project("proj_a")

    ops.force_validation(wo["id"], reason="re-judge the escalation on the live head")
    opened = store.get_work_order(wo["id"])
    assert opened["needs_attention"] == 0 and not opened["attention_reason"]

    judge(fleet, store, Panel("passed"))
    fleet.check_invariants(spec, store)

    settled = store.get_work_order(wo["id"])
    assert settled["status"] == "waiting_pr_merge"
    assert settled["needs_attention"] == 0, settled["attention_reason"]
    assert not settled["attention_reason"]


def test_a_forced_round_that_gives_up_again_flags_the_user_afresh(fleet, project,
                                                                  fake_gh):
    """The boundary on the test above"""
    store = ProjectStore(project)
    wo = escalated(fleet, store, project, fake_gh)
    spec = fleet.catalog.project("proj_a")

    ops.force_validation(wo["id"], reason="try the live head")
    judge(fleet, store, Panel("escalated"))
    fleet.check_invariants(spec, store)

    again = store.get_work_order(wo["id"])
    assert again["status"] == "needs_review"
    assert again["needs_attention"] == 1 and again["attention_reason"]


# -- the command, driven the way the operator drives it


def test_the_cli_opens_the_round_and_reports_where_the_order_went(fleet, project,
                                                                  fake_gh, capsys):
    """`args.validation_cmd` dispatch, end to end"""
    store, wo = parked(fleet, project)
    artifact(fake_gh)

    assert cli.main(["validation", "force", wo["id"],
                     "--reason", "round 1 predates head_sha"]) == 0

    out = capsys.readouterr().out
    assert "round 2 opened by hand — round 1 predates head_sha" in out
    assert "was waiting_pr_merge, now validating" in out
    forced = store.latest_validation_round(wo_id=wo["id"])
    assert forced is not None and forced["round"] == 2
    assert forced["forced_reason"] == "round 1 predates head_sha"


def test_the_cli_refuses_without_a_reason_and_opens_nothing(fleet, project, fake_gh):
    """`--reason` is `required=True`, so argparse exits 2 before `ops` is reached"""
    store, wo = parked(fleet, project)
    with pytest.raises(SystemExit) as caught:
        cli.main(["validation", "force", wo["id"]])
    assert caught.value.code == 2
    assert len(store.validation_rounds(wo_id=wo["id"])) == 1


def test_the_cli_hands_back_the_round_as_json(fleet, project, fake_gh, capsys):
    """The machine-readable half, because a forced round is a thing a script may want to follow"""
    store, wo = parked(fleet, project)
    artifact(fake_gh)

    assert cli.main(["--json", "validation", "force", wo["id"],
                     "--reason", "round 1 predates head_sha"]) == 0

    payload = json.loads(capsys.readouterr().out)
    forced = store.latest_validation_round(wo_id=wo["id"])
    assert forced is not None
    assert payload["round_id"] == forced["id"] and payload["round"] == 2
    assert payload["was"] == "waiting_pr_merge" and payload["status"] == "validating"
    assert payload["reason"] == "round 1 predates head_sha"


def test_a_work_order_that_does_not_exist_is_refused(fleet, project):
    with pytest.raises(ops.OpsError, match="not found"):
        ops.force_validation("wo-deleted", reason="re-judge it")


def test_the_panel_being_off_refuses_rather_than_opening_a_round_nothing_judges(
        tmp_path, jarvis_home, fake_claude, project, fake_gh):
    """`os.validation.enabled` is read at the SUBMISSION SITES ONLY, and this is a new submission site"""
    catalog_path = write_catalog(tmp_path, project)
    ops.start_os(str(catalog_path), foreground=True)
    daemon = Daemon(load_catalog(catalog_path))
    store, wo = parked(daemon, project)

    data = json.loads(catalog_path.read_text())
    data["os"]["validation"]["enabled"] = False
    catalog_path.write_text(json.dumps(data))

    with pytest.raises(ops.OpsError, match="validation panel is off"):
        ops.force_validation(wo["id"], reason="re-judge it")
    assert len(store.validation_rounds(wo_id=wo["id"])) == 1
