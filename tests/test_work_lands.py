"""Settling a work order asserts the deliverable (GitHub issue #232)"""

from __future__ import annotations

import json
import os
import subprocess
from pathlib import Path

import pytest

from jarvis import invariants, landing, ops
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.project_store import ProjectStore

PR = "https://github.com/acme/proj/pull/7"
OTHER_PR = "https://github.com/acme/proj/pull/8"
THIRD_PR = "https://github.com/acme/proj/pull/9"
#: A HIGHER number than `PR`, and that is the whole of it: `wo-cd73c537` had #116 opened on the branch...
LATER_PR = "https://github.com/acme/proj/pull/116"


def _git(cwd: Path, *args: str) -> str:
    env = {"HOME": str(cwd), "PATH": os.environ.get("PATH", ""),
           "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@t",
           "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@t",
           "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null"}
    return subprocess.run(["git", "-C", str(cwd), *args], check=True, env=env,
                          capture_output=True, text=True).stdout


def _feature(salt: str, n: int = 30) -> str:
    return "".join(f"def {salt}_helper_{i}(argument):  # {salt} feature line {i}\n"
                   f"    return compute_{salt}_result({i}, argument)\n"
                   for i in range(n))


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    """The shipped `project` fixture, upgraded into a repository with a real history"""
    return _boot(project, catalog_file)


@pytest.fixture()
def validating(jarvis_home, fake_claude, catalog_file, project):
    """`started`, with the validation panel switched ON"""
    data = json.loads(catalog_file.read_text())
    data["os"]["validation"] = {"enabled": True}
    catalog_file.write_text(json.dumps(data))
    return _boot(project, catalog_file)


def _boot(project: Path, catalog_file: Path) -> Daemon:
    _git(project, "symbolic-ref", "HEAD", "refs/heads/trunk")
    (project / "app.py").write_text(_feature("base", 10))
    _git(project, "add", "-A")
    _git(project, "commit", "-qm", "base")
    # Named `acme/proj` so `github.checked_pr_url` accepts the `PR` constant below: it refuses a pull...
    origin = project.parent / "acme" / "proj.git"
    origin.parent.mkdir(parents=True, exist_ok=True)
    _git(project.parent, "init", "--bare", "-q", str(origin))
    _git(project, "remote", "add", "origin", str(origin))
    _git(project, "push", "-q", "origin", "trunk")
    _git(project, "remote", "set-head", "origin", "trunk")
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


def _order(project: Path, title: str = "add feature X", *, code: str | None = None,
           dirty: str | None = None) -> dict:
    """A work order with a worktree, carrying `code` as a commit and `dirty` uncommitted"""
    wo = ops.create_work_order("proj_a", title)
    wt = project / ".claude" / "worktrees" / wo["id"]
    _git(project, "worktree", "add", "-q", "-b", f"worktree-{wo['id']}", str(wt),
         "trunk")
    store = ProjectStore(project)
    try:
        store.update_work_order(wo["id"], worktree=wo["id"])
    finally:
        store.close()
    if code:
        (wt / f"{code}.py").write_text(_feature(code))
        _git(wt, "add", "-A")
        _git(wt, "commit", "-qm", f"add {code}")
    if dirty:
        (wt / f"{dirty}.py").write_text(_feature(dirty))
    return {**wo, "worktree_path": wt}


def _settle(project: Path, *wo_ids: str) -> None:
    """Put work orders into `completed` the way the six stranded ones got there"""
    store = ProjectStore(project)
    try:
        for wo_id in wo_ids:
            store.set_status(wo_id, "completed")
    finally:
        store.close()


def _events(project: Path, wo_id: str, kind: str) -> list[dict]:
    store = ProjectStore(project)
    try:
        return store.events_of_kind(wo_id, kind)
    finally:
        store.close()


def _row(project: Path, wo_id: str) -> dict:
    store = ProjectStore(project)
    try:
        return store.get_work_order(wo_id)
    finally:
        store.close()


# -- Mode A: the pull request named in prose, and the --pr that was not passed


def test_finish_over_commits_with_no_pr_is_refused_and_names_the_url_in_the_summary(
        started, project):
    """Four of the six stranded orders finished with a summary naming a draft PR"""
    wo = _order(project, code="launcher")

    with pytest.raises(ops.OpsError) as e:
        ops.finish(wo["id"], f"Opened a draft PR at {PR} for review.")

    assert PR in str(e.value)
    assert "--pr" in str(e.value)
    # The record is untouched: a summary saved beside a refusal reads afterwards like a work order that...
    assert _row(project, wo["id"])["result_summary"] in (None, "")
    assert _row(project, wo["id"])["status"] != "completed"


def test_the_same_finish_with_pr_parks_it_and_never_reads_the_worktree(started, project):
    """The pairing"""
    wo = _order(project, code="launcher")

    out = ops.finish(wo["id"], "opened a PR", pr_url=PR)

    assert out["status"] == "waiting_pr_merge"
    assert _row(project, wo["id"])["pr_url"] == PR


def test_an_order_that_produced_no_code_finishes_exactly_as_before(started, project):
    """THE NEGATIVE CONTROL THAT DECIDES WHETHER THIS SHIPS"""
    wo = _order(project, "answer a question")

    assert ops.finish(wo["id"], "no code needed")["status"] == "completed"


def test_uncommitted_work_alone_is_enough_to_refuse(started, project):
    """A worker that never committed has still produced the thing that gets lost"""
    wo = _order(project, dirty="scratch")

    with pytest.raises(ops.OpsError) as e:
        ops.finish(wo["id"], "done")

    assert "uncommitted" in str(e.value)


# -- the escape: an abandonment is a legitimate outcome, cheaply recorded


def test_abandon_completes_the_order_and_writes_down_what_was_dropped(started, project):
    """The goal is that the decision is WRITTEN DOWN, not that it is prevented"""
    wo = _order(project, code="spike")

    out = ops.finish(wo["id"], "spiked it; not worth landing",
                     abandon="the approach does not work, see the summary")

    assert out["status"] == "completed"
    [event] = _events(project, wo["id"], "abandoned")
    payload = json.loads(event["payload"])
    assert payload["reason"] == "the approach does not work, see the summary"
    assert payload["commits"] == 1
    assert payload["branch"] == f"worktree-{wo['id']}"


def test_the_remedy_the_sweep_prints_works_on_an_order_already_completed(started,
                                                                          project,
                                                                          fake_gh):
    """INV-WORK-LANDED tells the user to run `wo finish --abandon` on a COMPLETED order"""
    wo = _delivered(project, "launcher contract", PR, code="launcher")
    fake_gh.set_pr(PR, "OPEN")
    _refresh(started, project)
    [violation] = _violations(project)
    assert f"finish {wo['id']}" in violation.detail and "--abandon" in violation.detail

    ops.finish(wo["id"], "dropping it", abandon="superseded by the rewrite")

    assert _row(project, wo["id"])["status"] == "completed"
    assert _violations(project) == []


def test_superseded_by_links_a_duplicate_to_the_order_carrying_its_work(started, project):
    """Issue #1307: Morpheus follows the link from a confirmed failure's fix order"""
    dup, real, third = _order(project), _order(project), _order(project)
    with pytest.raises(ops.OpsError):
        ops.finish(dup["id"], "duplicate", superseded_by=dup["id"])
    with pytest.raises(ops.OpsError):
        ops.finish(dup["id"], "duplicate", superseded_by="wo-00000000")
    ops.finish(dup["id"], "duplicate", superseded_by=real["id"])
    ops.finish(real["id"], "duplicate too", superseded_by=third["id"])
    ops.finish(third["id"], "loops back", superseded_by=dup["id"])

    assert ops.fix_carrier(dup["id"]) == third["id"]  # the cycle stops at the first repeat
    assert ops.fix_carrier("wo-00000000") == "wo-00000000"


def test_an_abandonment_is_superseded_by_a_later_ordinary_finish(started, project):
    """The newest of the two events wins, or an order excused once is excused for ever"""
    wo = _order(project, code="spike")
    ops.finish(wo["id"], "dropping it", abandon="not worth landing")

    with pytest.raises(ops.OpsError):
        ops.finish(wo["id"], "actually I did land it")


# -- Mode B: accepting assumptions is not a closure verb


def test_accepting_assumptions_will_not_complete_an_order_holding_unlanded_commits(
        started, project):
    """THE SHARP EDGE. wo-69a06ff4 and wo-01d30340 both died here"""
    wo = _order(project, code="watchdogs")
    ops.assume(wo["id"], "polled every 30s rather than every tick")
    store = ProjectStore(project)
    try:
        store.set_status(wo["id"], "needs_review")
        store.flag_attention(wo["id"], "worker idle without jarvis wo finish")
    finally:
        store.close()

    out = ops.review_work_order(wo["id"], accept=True)

    assert out["status"] == "needs_review"
    assert out["reviewed"] == 1          # the assumptions WERE decided: that is its job
    row = _row(project, wo["id"])
    assert row["status"] == "needs_review"
    assert row["attention_reason"] == invariants.UNLANDED_BLOCKER
    # The evidence is preserved rather than written over, which is the whole of Mode B
    import json
    payload = json.loads(_events(project, wo["id"], "work_unlanded")[-1]["payload"])
    assert payload["commits"] == 1
    assert payload["branch"] == f"worktree-{wo['id']}"


def test_accepting_assumptions_completes_an_order_that_produced_no_code(started, project):
    """The pairing: the review still does what it always did when nothing is stranded"""
    wo = _order(project, "decide something")
    ops.assume(wo["id"], "used the existing helper")
    store = ProjectStore(project)
    try:
        store.set_status(wo["id"], "needs_review")
    finally:
        store.close()

    assert ops.review_work_order(wo["id"], accept=True)["status"] == "completed"


def test_a_merged_pull_request_is_not_re_flagged_by_its_own_commits(started, project):
    """THE FALSE POSITIVE THAT WOULD ARRIVE BY THE BACK DOOR"""
    wo = _order(project, code="merged_feature")
    ops.assume(wo["id"], "named it merged_feature")
    store = ProjectStore(project)
    try:
        store.update_work_order(wo["id"], pr_url=PR, pr_state="MERGED")
        store.set_status(wo["id"], "needs_review")
    finally:
        store.close()

    assert ops.review_work_order(wo["id"], accept=True)["status"] == "completed"


def test_the_parked_flag_survives_a_reconcile_tick(started, project):
    """kn-eafe383a's rule, one level over: a flag `true_blockers` cannot re-derive is relabelled by..."""
    wo = _order(project, code="watchdogs")
    ops.assume(wo["id"], "polled every 30s")
    store = ProjectStore(project)
    try:
        store.set_status(wo["id"], "needs_review")
    finally:
        store.close()
    ops.review_work_order(wo["id"], accept=True)

    store = ProjectStore(project)
    try:
        invariants.check_project(store, repair=True)
        assert store.get_work_order(wo["id"])["attention_reason"] == \
            invariants.UNLANDED_BLOCKER
    finally:
        store.close()

    ops.finish(wo["id"], "opened the PR after all", pr_url=PR)

    store = ProjectStore(project)
    try:
        assert store.work_unlanded_open(wo["id"]) is False
        assert invariants.UNLANDED_BLOCKER not in invariants.true_blockers(
            store, store.get_work_order(wo["id"]))
    finally:
        store.close()


def test_wo_done_records_the_unlanded_work_and_still_closes_it(started, project):
    """The one landing that does NOT refuse, and the reason is that it is not silence (#232)"""
    wo = _order(project, code="stranded")

    assert ops.mark_done(wo["id"])["status"] == "completed"

    import json
    payload = json.loads(_events(project, wo["id"], "work_unlanded")[-1]["payload"])
    assert payload["commits"] == 1
    assert payload["closed_by"] == "marked_done"


# -- the standing report: jarvis doctor


def _violations(project: Path) -> list[invariants.Violation]:
    store = ProjectStore(project)
    try:
        return [v for v in invariants.check_project(store, repair=True, slow=True)
                if v.invariant == "INV-WORK-LANDED"]
    finally:
        store.close()


def _refresh(daemon: Daemon, project: Path) -> None:
    """One landing sweep's GitHub pass — `Daemon.refresh_landings`"""
    store = ProjectStore(project)
    try:
        daemon.refresh_landings(daemon.catalog.project("proj_a"), store)
    finally:
        store.close()


def _pr_reads(fake_gh) -> list[str]:
    """Every pull request this test has caused the sweep to read"""
    return [c["argv"][2] for c in fake_gh.calls if c["argv"][:2] == ["pr", "view"]]


def _delivered(project: Path, title: str, pr_url: str, code: str = "code") -> dict:
    """A COMPLETED work order carrying `pr_url` — the sweep's whole population"""
    wo = _order(project, title, code=code)
    store = ProjectStore(project)
    try:
        store.update_work_order(wo["id"], pr_url=pr_url)
    finally:
        store.close()
    _settle(project, wo["id"])
    return wo


def test_a_merged_pull_request_is_the_answer_and_an_open_one_is_a_violation(
        started, project, fake_gh):
    """THE NARROWED PREDICATE, all three reportable states in one sweep"""
    merged = _delivered(project, "the cap", PR, code="cap")
    waiting = _delivered(project, "launcher contract", OTHER_PR, code="launcher")
    refused = _delivered(project, "superseded", THIRD_PR, code="superseded")
    fake_gh.set_pr(PR, "MERGED")
    fake_gh.set_pr(OTHER_PR, "OPEN")
    fake_gh.set_pr(THIRD_PR, "CLOSED")

    _refresh(started, project)
    found = {v.wo_id: v for v in _violations(project)}

    assert set(found) == {waiting["id"], refused["id"]}
    assert found[waiting["id"]].context["verdict"] == landing.AWAITING_MERGE
    assert "waiting on a merge" in found[waiting["id"]].detail
    assert "Merge it, or record the decision to drop it" in found[waiting["id"]].detail
    assert found[refused["id"]].context["verdict"] == landing.REFUSED
    assert "delivered and refused" in found[refused["id"]].detail
    assert "Re-open and merge it" in found[refused["id"]].detail
    assert not found[waiting["id"]].repaired    # what to do with it is the user's call


def test_an_order_with_no_recorded_pull_request_is_silent_and_costs_no_round_trip(
        started, project, fake_gh):
    """THE LAYERING, stated where it takes effect"""
    committed = _order(project, "never opened a pull request", code="orphan")
    planner = ops.create_work_order("proj_a", "wrote a plan")
    _settle(project, committed["id"], planner["id"])
    assert _row(project, committed["id"])["pr_url"] is None

    _refresh(started, project)

    assert _violations(project) == []
    assert _pr_reads(fake_gh) == []


def test_the_check_judges_the_recorded_pull_request_and_not_a_newer_one(
        started, project, fake_gh):
    """wo-cd73c537's shape, and A KNOWN CONSEQUENCE OF THE LAYERING rather than a defect (#81)"""
    wo = _delivered(project, "the validation layer", PR, code="validation")
    fake_gh.set_pr(PR, "MERGED")
    fake_gh.set_pr(LATER_PR, "OPEN")

    _refresh(started, project)

    assert _violations(project) == []
    assert _pr_reads(fake_gh) == [PR]           # the recorded one, and only it


def test_a_merged_pull_request_with_a_tail_on_its_branch_is_no_longer_reported(
        started, project, fake_gh):
    """THE SECOND THING THE NARROWING GIVES UP, and it is worth stating as a test (#232)"""
    wo = _order(project, "launcher contract", code="launcher")
    ops.finish(wo["id"], "opened a PR", pr_url=PR)
    fake_gh.set_pr(PR, "MERGED", merged_at="2026-08-02T10:00:00Z",
                   head_oid=_git(wo["worktree_path"], "rev-parse", "HEAD").strip())
    store = ProjectStore(project)
    try:
        started.poll_pull_requests(started.catalog.project("proj_a"), store)
    finally:
        store.close()
    assert _row(project, wo["id"])["status"] == "completed"
    (wo["worktree_path"] / "onboarding.py").write_text(_feature("onboarding"))
    _git(wo["worktree_path"], "add", "-A")
    _git(wo["worktree_path"], "commit", "-qm", "the onboarding half")

    _refresh(started, project)

    assert _violations(project) == []


def test_complete_merged_writes_the_sha_that_merged_onto_the_event(started, project):
    """WHICH COMMIT merged, on the record, because the event IS the record"""
    wo = _order(project, "launcher contract", code="launcher")
    ops.finish(wo["id"], "opened a PR", pr_url=PR)
    sha = "9f1c2ab4d5e6f708192a3b4c5d6e7f8091a2b3c4"

    store = ProjectStore(project)
    try:
        ops.complete_merged(store, store.get_work_order(wo["id"]),
                            merged_at="2026-08-02T10:00:00Z", head_oid=sha)
    finally:
        store.close()

    [event] = _events(project, wo["id"], "pr_merged")
    assert json.loads(event["payload"])["head_oid"] == sha


def _completions(project: Path, wo_id: str) -> list[dict]:
    store = ProjectStore(project)
    try:
        return [n for n in store.unrouted_notifications()
                if n["wo_id"] == wo_id and n["source"] == "completion"]
    finally:
        store.close()


def test_a_user_order_that_merges_tells_the_user_with_its_final_message(started, project):
    """Issue 1385: wo-d83e989e's final message offered two follow-ups and nobody heard"""
    wo = _order(project, "launcher contract", code="launcher")
    ops.finish(wo["id"], "opened a PR", pr_url=PR)
    store = ProjectStore(project)
    try:
        store.record_agent_reply(wo["id"], "Done. Two gaps remain; want me to file them?")
        ops.complete_merged(store, store.get_work_order(wo["id"]))
    finally:
        store.close()

    [note] = _completions(project, wo["id"])
    assert "pull request merged" in note["title"]
    assert "opened a PR" in note["body"] and PR in note["body"]
    assert "want me to file them?" in note["body"]


def test_an_order_the_os_opened_completes_without_an_inbox_item(started, project):
    wo = _order(project, "launcher contract", code="launcher")
    ops.finish(wo["id"], "opened a PR", pr_url=PR)
    store = ProjectStore(project)
    try:
        store.update_work_order(wo["id"], origin="os")
        ops.complete_merged(store, store.get_work_order(wo["id"]))
    finally:
        store.close()

    assert _completions(project, wo["id"]) == []


def test_the_sweep_records_the_merge_it_saw_and_never_settles_the_order(
        started, project, fake_gh):
    """The merge, on the record, without `pr_state` gaining a writer — 2026-09-25 spec §5"""
    sha = "9f1c2ab4d5e6f708192a3b4c5d6e7f8091a2b3c4"
    wo = _delivered(project, "the cap", PR, code="cap")
    fake_gh.set_pr(PR, "MERGED", merged_at="2026-09-20T10:00:00Z", head_oid=sha)

    _refresh(started, project)

    [event] = _events(project, wo["id"], "pr_merged")
    payload = json.loads(event["payload"])
    assert payload["pr_url"] == PR
    assert payload["head_oid"] == sha
    assert payload["merged_at"] == "2026-09-20T10:00:00Z"
    # Distinguishable from the merge that ENDED an order (`complete_merged`)
    assert payload["source"] == "landing_sweep"
    store = ProjectStore(project)
    try:
        fresh = store.get_work_order(wo["id"])
    finally:
        store.close()
    assert fresh["status"] == "completed"
    assert fresh["pr_state"] is None    # kn-dbc4971d: no new writer
    assert _events(project, wo["id"], "landing_seen")


def test_the_sweep_records_the_merge_once_and_never_a_second_time(
        started, project, fake_gh):
    """`timeline.build_timeline` renders every `pr_merged` it finds: two events are two merge lines on..."""
    wo = _delivered(project, "the cap", PR, code="cap")
    fake_gh.set_pr(PR, "MERGED", head_oid="abc1234")
    _refresh(started, project)

    store = ProjectStore(project)
    try:
        store.conn.execute(
            "UPDATE wo_events SET ts = ts - ? WHERE wo_id = ? AND kind = 'landing_seen'",
            (landing.FRESH_FOR_SECONDS + 1, wo["id"]))
        store.conn.commit()
    finally:
        store.close()
    _refresh(started, project)

    assert len(_pr_reads(fake_gh)) == 2      # it did ask again…
    assert len(_events(project, wo["id"], "pr_merged")) == 1     # …and wrote once


def _gate_records(project: Path, wo_id: str, pr_url: str) -> None:
    """Put `pr_url` on the record the way a planner's gets there — an approved merge gate"""
    from jarvis import gates

    store = ProjectStore(project)
    try:
        approval = store.add_approval(wo_id, gates.PR_MERGE,
                                      f"gh pr merge {pr_url} --squash")
        gates.apply_decision(store, approval["id"], verdict="approved",
                             reason="checks green", decided_by="neo")
        assert store.get_work_order(wo_id)["pr_url"] == pr_url
    finally:
        store.close()


def test_the_sweep_reads_the_pull_request_a_gate_recorded(started, project, fake_gh):
    """THE SEAM: the sweep keeps reading the RAW column, and must (#742)"""
    sha = "9f3c1ad20b4e5f6a7b8c9d0e1f2a3b4c5d6e7f80"
    wo = _order(project, "plan the feature", code="plan")
    _gate_records(project, wo["id"], PR)
    _settle(project, wo["id"])
    fake_gh.set_pr(PR, "MERGED", merged_at="2026-09-25T10:00:00Z", head_oid=sha)

    _refresh(started, project)

    [event] = _events(project, wo["id"], "pr_merged")
    payload = json.loads(event["payload"])
    assert payload["head_oid"] == sha
    assert payload["source"] == "landing_sweep"
    assert _row(project, wo["id"])["status"] == "completed"


def test_the_merge_the_sweep_saw_answers_a_park_over_the_same_work(started, project,
                                                                  fake_gh):
    """§6's INTENDED consequence, and the one reader the second `pr_merged` writer changes (#742)"""
    wo = _order(project, code="watchdogs")
    ops.assume(wo["id"], "polled every 30s")
    store = ProjectStore(project)
    try:
        store.set_status(wo["id"], "needs_review")
    finally:
        store.close()
    ops.review_work_order(wo["id"], accept=True)        # parks it: no pull request
    store = ProjectStore(project)
    try:
        assert store.work_unlanded_open(wo["id"]) is True
    finally:
        store.close()

    _gate_records(project, wo["id"], PR)
    _settle(project, wo["id"])
    fake_gh.set_pr(PR, "MERGED", head_oid="abc1234")
    _refresh(started, project)

    store = ProjectStore(project)
    try:
        assert store.work_unlanded_open(wo["id"]) is False
        assert invariants.UNLANDED_BLOCKER not in invariants.true_blockers(
            store, store.get_work_order(wo["id"]))
    finally:
        store.close()


def test_a_pull_request_that_has_not_merged_records_no_merge(started, project, fake_gh):
    wo = _delivered(project, "launcher contract", OTHER_PR, code="launcher")
    fake_gh.set_pr(OTHER_PR, "OPEN")

    _refresh(started, project)

    assert _events(project, wo["id"], "pr_merged") == []


def test_both_surfaces_say_the_pull_request_merged_off_the_same_derivation(
        started, project, fake_gh, capsys):
    """The issue's Expected: `pr_url …/pull/735` with state MERGED, on `wo show`"""
    from jarvis import cli

    wo = _delivered(project, "the cap", PR, code="cap")
    fake_gh.set_pr(PR, "MERGED", head_oid="abc1234")
    _refresh(started, project)

    assert cli.main(["wo", "show", wo["id"], "--json"]) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["pr_url"] == PR
    assert document["merge_state"]["state"] == "MERGED"
    assert document["merge_state"]["head_oid"] == "abc1234"

    assert cli.main(["wo", "show", wo["id"]]) == 0
    assert "MERGED" in capsys.readouterr().out


def test_an_order_whose_pull_request_nothing_has_looked_at_claims_no_state(
        started, project, capsys):
    """"Nothing has looked yet" is not "did not merge" — `landing.py`'s own rule"""
    from jarvis import cli

    wo = _order(project, "launcher contract", code="launcher")
    ops.finish(wo["id"], "opened a PR", pr_url=PR)

    assert cli.main(["wo", "show", wo["id"], "--json"]) == 0
    document = json.loads(capsys.readouterr().out)
    assert document["merge_state"] == {"pr_url": PR, "state": "", "head_oid": "",
                                       "merged_at": None, "source": ""}

    # ...and the human surface says the same thing: the link, and NO state beside it
    assert cli.main(["wo", "show", wo["id"]]) == 0
    printed = capsys.readouterr().out
    assert PR in printed
    assert "MERGED" not in printed


def test_an_order_with_no_pull_request_gains_no_merge_line_at_all(started, project,
                                                                 capsys):
    from jarvis import cli

    wo = _order(project, "a planner", code="")
    ops.finish(wo["id"], "wrote no code")

    assert cli.main(["wo", "show", wo["id"], "--json"]) == 0
    assert "merge_state" not in json.loads(capsys.readouterr().out)


def test_the_check_is_silent_until_the_sweep_has_run_and_never_guesses(
        started, project, fake_gh):
    """The invariant is a pure timeline read, so an order nothing has looked at yet is silent"""
    wo = _delivered(project, "launcher contract", PR, code="launcher")
    fake_gh.set_pr(PR, "OPEN")

    assert _violations(project) == []
    assert _events(project, wo["id"], "landing_seen") == []

    _refresh(started, project)

    assert [v.wo_id for v in _violations(project)] == [wo["id"]]


def test_the_sweep_is_silent_on_abandoned_orders_and_never_asks_about_them(
        started, project, fake_gh):
    """An abandonment is a decision that was taken"""
    dropped = _delivered(project, "spiked it", PR, code="spike")
    ops.finish(dropped["id"], "not landing this", abandon="the approach does not work")
    fake_gh.set_pr(PR, "OPEN")

    _refresh(started, project)

    assert _violations(project) == []
    assert _pr_reads(fake_gh) == []


def test_a_settled_reading_is_not_re_asked_every_sweep_and_an_unsettled_one_is(
        started, project, fake_gh):
    """The TTL, which is what stops the sweep costing a round trip per completed order per hour for ever"""
    landed = _delivered(project, "the cap", PR, code="cap")
    waiting = _delivered(project, "launcher contract", OTHER_PR, code="launcher")
    fake_gh.set_pr(PR, "MERGED")
    fake_gh.set_pr(OTHER_PR, "OPEN")

    _refresh(started, project)
    assert sorted(_pr_reads(fake_gh)) == [PR, OTHER_PR]
    _refresh(started, project)

    # The open one alone; the merged one is inside its TTL
    assert _pr_reads(fake_gh)[2:] == [OTHER_PR]

    store = ProjectStore(project)
    try:
        store.conn.execute(
            "UPDATE wo_events SET ts = ts - ? WHERE wo_id = ? AND kind = 'landing_seen'",
            (landing.FRESH_FOR_SECONDS + 1, landed["id"]))
        store.conn.commit()
    finally:
        store.close()
    fake_gh.set_pr(PR, "CLOSED")
    _refresh(started, project)

    assert {v.wo_id for v in _violations(project)} == {landed["id"], waiting["id"]}


def test_one_sweep_asks_about_at_most_the_cap_and_the_rest_arrive_next_time(
        started, project, fake_gh, monkeypatch):
    """The cap is about the TICK"""
    monkeypatch.setattr(landing, "REFRESH_PER_SWEEP", 2)
    urls = [PR, OTHER_PR, THIRD_PR]
    orders = [_delivered(project, f"order {n}", urls[n], code=f"code{n}")
              for n in range(3)]
    for url in urls:
        fake_gh.set_pr(url, "MERGED")

    _refresh(started, project)
    assert len(_pr_reads(fake_gh)) == 2

    _refresh(started, project)

    # The third one, and no re-ask of the two already settled as `landed`
    assert len(_pr_reads(fake_gh)) == 3
    assert set(_pr_reads(fake_gh)) == set(urls)
    assert len(orders) == 3


def test_a_gh_that_cannot_answer_leaves_the_last_record_standing(
        started, project, fake_gh):
    """A broken poll must make the sweep STALE, never wrong"""
    wo = _delivered(project, "launcher contract", PR, code="launcher")
    fake_gh.set_pr(PR, "OPEN")
    _refresh(started, project)
    assert [v.wo_id for v in _violations(project)] == [wo["id"]]

    fake_gh.fail("gh: could not read credentials")
    _refresh(started, project)

    # Still reported, off the record the last sweep left behind — not dropped, not re-judged
    assert [v.wo_id for v in _violations(project)] == [wo["id"]]
    assert len(_events(project, wo["id"], "landing_seen")) == 1


def test_the_read_only_doctor_gives_the_same_answer_and_writes_nothing(
        started, project, fake_gh):
    """What the plain `jarvis doctor` a human types does"""
    waiting = _delivered(project, "launcher contract", PR, code="launcher")
    landed = _delivered(project, "the cap", OTHER_PR, code="cap")
    fake_gh.set_pr(PR, "OPEN")
    fake_gh.set_pr(OTHER_PR, "MERGED")
    _refresh(started, project)

    store = ProjectStore(project)
    try:
        before = len(store.events_of_kind(waiting["id"], "landing_seen"))
        found = [v for v in invariants.check_project(store, repair=False, slow=True)
                 if v.invariant == "INV-WORK-LANDED"]
        after = len(store.events_of_kind(waiting["id"], "landing_seen"))
    finally:
        store.close()

    assert [v.wo_id for v in found] == [waiting["id"]]
    assert before == after == 1
    assert [v.wo_id for v in _violations(project)] == [waiting["id"]]
    assert landed["id"] not in {v.wo_id for v in found}


# -- the remedy has to CLEAR the alert, and for months it did not


def test_wo_done_clears_the_alert_when_the_worktree_is_gone_but_the_branch_is_not(
        started, project, fake_gh):
    """THE SECOND DEFECT, measured on the live records on 2026-09-18 and fixed here"""
    wo = _delivered(project, "will never merge", PR, code="orphaned")
    fake_gh.set_pr(PR, "OPEN")
    _refresh(started, project)
    assert [v.wo_id for v in _violations(project)] == [wo["id"]]
    _git(project, "worktree", "remove", "--force", str(wo["worktree_path"]))
    assert not wo["worktree_path"].exists()
    assert _git(project, "rev-parse", "--verify", f"worktree-{wo['id']}").strip()

    assert ops.mark_done(wo["id"])["status"] == "completed"

    payload = json.loads(_events(project, wo["id"], "work_unlanded")[-1]["payload"])
    assert payload["closed_by"] == "marked_done"
    assert payload["pr_url"] == PR      # what was actually unlanded, not a stale branch
    _refresh(started, project)
    assert _violations(project) == []


def test_wo_done_over_a_merged_pull_request_records_no_unlanded_work(
        started, project, fake_gh):
    """The pairing, and the thing "record it whenever there is a pr_url" would get wrong"""
    wo = _delivered(project, "the cap", PR, code="cap")
    fake_gh.set_pr(PR, "MERGED")
    _refresh(started, project)

    assert ops.mark_done(wo["id"])["status"] == "completed"

    assert _events(project, wo["id"], "work_unlanded") == []


def test_abandoning_an_already_settled_order_opens_no_validation_round(
        validating, project, fake_gh):
    """THE OTHER HALF OF THE SECOND DEFECT, and it is what actually happened"""
    wo = _delivered(project, "superseded", PR, code="superseded")
    fake_gh.set_pr(PR, "CLOSED")
    _refresh(validating, project)
    assert [v.wo_id for v in _violations(project)] == [wo["id"]]

    out = ops.finish(wo["id"], "dropping it", abandon="superseded by the rewrite")

    assert out["status"] == "completed"
    row = _row(project, wo["id"])
    assert row["status"] == "completed"
    assert row["needs_attention"] == 0
    assert _events(project, wo["id"], "validation_submitted") == []
    assert _events(project, wo["id"], "validation_escalated") == []
    store = ProjectStore(project)
    try:
        assert store.validation_rounds(wo_id=wo["id"]) == []
    finally:
        store.close()
    _refresh(validating, project)
    assert _violations(project) == []


def test_a_live_order_abandoning_still_goes_through_validation(validating, project):
    """The pairing"""
    wo = _order(project, "spiked it", code="spike")

    ops.finish(wo["id"], "spiked it; not worth landing",
               abandon="the approach does not work")

    assert len(_events(project, wo["id"], "validation_submitted")) == 1


# -- an audit with no data must not read as a clean bill of health


def _all_violations(project: Path) -> list[invariants.Violation]:
    """Everything `jarvis doctor` would print, not just INV-WORK-LANDED"""
    store = ProjectStore(project)
    try:
        return list(invariants.check_project(store, repair=True, slow=True))
    finally:
        store.close()


def test_an_order_nobody_has_read_yet_is_reported_as_unread_not_as_landed(
        started, project, fake_gh):
    """REVIEW ROUND 1'S FINDING, and the sharpest one in this change"""
    wo = _delivered(project, "launcher contract", PR, code="launcher")
    fake_gh.set_pr(PR, "OPEN")

    found = [v for v in _all_violations(project)
             if v.invariant == "INV-LANDING-AUDIT-FRESH"]

    assert len(found) == 1
    assert found[0].wo_id is None            # it is about the project, not one order
    assert found[0].context == {"unchecked": 1, "population": 1}
    assert "NOT the same as saying they landed" in found[0].detail
    assert _violations(project) == []        # ...and INV-WORK-LANDED still says nothing
    assert wo["id"]


def test_the_unread_report_clears_once_the_sweep_has_run(started, project, fake_gh):
    """The pairing, and what makes the line worth printing rather than nagging"""
    _delivered(project, "the cap", PR, code="cap")
    _delivered(project, "launcher contract", OTHER_PR, code="launcher")
    fake_gh.set_pr(PR, "MERGED")
    fake_gh.set_pr(OTHER_PR, "MERGED")
    assert [v.context["unchecked"] for v in _all_violations(project)
            if v.invariant == "INV-LANDING-AUDIT-FRESH"] == [2]

    _refresh(started, project)

    assert [v for v in _all_violations(project)
            if v.invariant == "INV-LANDING-AUDIT-FRESH"] == []


def test_a_reading_older_than_the_freshness_window_counts_as_unread(
        started, project, fake_gh):
    """A DEAD DAEMON MUST NOT LOOK LIKE A CLEAN FLEET EITHER"""
    wo = _delivered(project, "the cap", PR, code="cap")
    fake_gh.set_pr(PR, "MERGED")
    _refresh(started, project)
    assert [v for v in _all_violations(project)
            if v.invariant == "INV-LANDING-AUDIT-FRESH"] == []

    store = ProjectStore(project)
    try:
        store.conn.execute(
            "UPDATE wo_events SET ts = ts - ? WHERE wo_id = ? AND kind = 'landing_seen'",
            (landing.FRESH_FOR_SECONDS + 1, wo["id"]))
        store.conn.commit()
    finally:
        store.close()

    found = [v for v in _all_violations(project)
             if v.invariant == "INV-LANDING-AUDIT-FRESH"]
    assert [v.context["unchecked"] for v in found] == [1]


def test_a_project_with_nothing_in_the_population_says_nothing(started, project):
    """The negative control"""
    planner = ops.create_work_order("proj_a", "wrote a plan")
    committed = _order(project, "never opened a pull request", code="orphan")
    _settle(project, planner["id"], committed["id"])

    assert [v for v in _all_violations(project)
            if v.invariant == "INV-LANDING-AUDIT-FRESH"] == []
