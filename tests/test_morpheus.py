"""Morpheus validates shipped PRs in production (docs/superpowers/specs/2026-10-21-morpheus-validates-releases-in-production.md)"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from jarvis import claude_cli, db, hooks, morpheus
from jarvis.catalog import Catalog, CatalogError, MorpheusConfig, OsConfig, parse_catalog
from jarvis.central_store import CentralStore
from jarvis.testing import TEST_EPOCH

pytestmark = pytest.mark.usefixtures("neo_projects")

REPO_ROOT = Path(__file__).resolve().parent.parent
DAY = 86400

HINTS_BODY = """## Summary
x

## How to validate in production

Watch the dashboard after the next release.

```bash
# exit 0 = validated
jarvis status --json | grep -q morpheus
```

## Screenshots
None.
"""


def body(section: str) -> str:
    return f"## Summary\nx\n\n## How to validate in production\n\n{section}\n\n## Screenshots\n-\n"


def pr(n: int = 7, text: str = HINTS_BODY, title: str = "[wo-1234abcd] thing") -> morpheus.PullRequestBody:
    return morpheus.PullRequestBody(number=n, url=f"https://github.com/o/r/pull/{n}",
                                    title=title, body=text)


@pytest.fixture()
def central():
    c = CentralStore()
    yield c
    c.close()


@pytest.fixture()
def cfg() -> MorpheusConfig:
    return MorpheusConfig()


def catalog(**morpheus_cfg) -> Catalog:
    return Catalog(os=OsConfig(morpheus=MorpheusConfig(**morpheus_cfg)), projects=[])


def queued(central, n: int = 7, text: str = HINTS_BODY, project: str = "proj_a",
           now: float | None = TEST_EPOCH, **kw) -> dict:
    return morpheus.enqueue(central, MorpheusConfig(), pr(n, text, **kw), project=project,
                            repo="o/r", now=now)


def monitoring(central, script: str = "exit 75", **kw) -> dict:
    item = queued(central, **kw)
    central.update_morpheus_item(item["id"], script=script, script_source="agent",
                                 state="monitoring", next_check_ts=TEST_EPOCH)
    return central.morpheus_item(item["id"])


# -- 1. the PR section: hints, never a script


def test_a_code_block_is_a_hint_and_its_comments_are_not_headings():
    raw = morpheus.find_section(HINTS_BODY)
    assert raw is not None and "Screenshots" not in raw
    section = morpheus.parse_section(raw)
    assert "grep -q morpheus" in section.text and "Watch the dashboard" in section.text
    assert not section.waived and not section.empty
    assert not hasattr(section, "script")


@pytest.mark.parametrize("text,reason", [("None — docs only", "docs only"),
                                         ("None.", "no reason given"),
                                         ("- None: tests only", "tests only")])
def test_none_waives_validation(text, reason):
    assert morpheus.parse_section(text).waived == reason


def test_prose_starting_with_none_is_not_a_waiver():
    assert morpheus.parse_section("None of the old paths change; watch the inbox.").waived == ""


def test_the_shipped_template_is_empty_and_its_comment_does_not_swallow_the_body():
    template = (REPO_ROOT / ".github/pull_request_template.md").read_text()
    raw = morpheus.find_section(template)
    assert raw is not None and "Screenshots" not in raw
    assert morpheus.parse_section(raw).empty
    assert morpheus.section_problem(template) == \
        f"`## {morpheus.SECTION}` is still the empty template"


def test_a_body_without_the_section_names_the_fix():
    assert "no `## How to validate in production` section" in \
        morpheus.section_problem("## Summary\nx\n")
    assert morpheus.section_problem(HINTS_BODY) is None


# -- 2. which PRs reached production


def test_pr_numbers_from_merge_and_squash_subjects():
    subjects = ["Merge pull request #12 from o/branch", "Fix the thing (#15)",
                "Release jarvis-0.1.1", "Merge pull request #12 from o/again", "no pr here"]
    assert morpheus.pr_numbers(subjects) == [12, 15]


def test_the_work_order_comes_from_the_title_prefix():
    assert morpheus.wo_of_title("[wo-1234abcd] thing") == "wo-1234abcd"
    assert morpheus.wo_of_title("thing [wo-1234abcd]") == ""
    assert morpheus.parse_pr_url("https://github.com/o/r/pull/9") == ("o/r", 9)


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture()
def release_repo(tmp_path) -> Path:
    repo = tmp_path / "rel"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    for subject, tag in (("Merge pull request #1 from o/a", "jarvis-0.1.0"),
                         ("Merge pull request #2 from o/b", None),
                         ("Squashed change (#3)", "jarvis-0.1.1")):
        _git(repo, "commit", "-q", "--allow-empty", "-m", subject)
        if tag:
            _git(repo, "tag", tag)
    return repo


def test_a_release_queues_only_what_it_added(central, cfg, release_repo):
    fetched = []

    def fetch(slug, n):
        fetched.append(n)
        return pr(n, body("None — docs") if n == 2 else HINTS_BODY, title=f"PR {n}")

    out = morpheus.enqueue_release(central, cfg, release_repo, "jarvis-0.1.1",
                                   project="jarvis_os", fetch=fetch, slug="o/r")
    assert out["from"] == "jarvis-0.1.0" and out["prs"] == [2, 3] and fetched == [2, 3]
    states = {i["pr_number"]: (i["state"], i["project"]) for i in central.morpheus_items()}
    assert states == {2: ("validated", "jarvis_os"), 3: ("queued", "jarvis_os")}
    assert central.get_state(morpheus.RELEASE_KEY) == "jarvis-0.1.1"
    # Idempotent: shipit and the daemon may both queue the same release
    again = morpheus.enqueue_release(central, cfg, release_repo, "jarvis-0.1.1",
                                     project="jarvis_os", fetch=fetch, slug="o/r")
    assert again["queued"] == [] and again["already_queued"] == [2, 3]


def test_one_unreadable_pr_does_not_drop_the_rest(central, cfg, release_repo):
    def fetch(slug, n):
        if n == 2:
            raise RuntimeError("gh broke")
        return pr(n)

    out = morpheus.enqueue_release(central, cfg, release_repo, "jarvis-0.1.1",
                                   project="jarvis_os", fetch=fetch, slug="o/r")
    assert out["errors"] == ["#2: gh broke"] and len(out["queued"]) == 1


def test_the_daemon_backstop_queues_the_running_release_once(central, cfg, release_repo):
    _git(release_repo, "checkout", "-q", "jarvis-0.1.1")
    calls = []
    fetch = lambda s, n: calls.append(n) or pr(n)  # noqa: E731
    kw = {"project": "jarvis_os", "fetch": fetch, "slug": "o/r"}
    assert morpheus.ensure_release_queued(central, cfg, release_repo,
                                          **kw)["release"] == "jarvis-0.1.1"
    assert morpheus.ensure_release_queued(central, cfg, release_repo, **kw) is None
    assert calls == [2, 3]


def test_a_dev_checkout_on_a_branch_queues_nothing(central, cfg, release_repo):
    _git(release_repo, "commit", "-q", "--allow-empty", "-m", "after the tag")
    assert morpheus.ensure_release_queued(central, cfg, release_repo, project="jarvis_os",
                                          fetch=lambda s, n: pr(n), slug="o/r") is None


def test_every_other_project_queues_on_merge_from_first_sight(central, cfg, project, clock):
    from jarvis.project_store import ProjectStore

    store = ProjectStore(project)
    try:
        old = store.create_work_order(title="before", description="d")
        store.add_event(old["id"], "pr_merged", {"pr_url": "https://github.com/o/r/pull/1"})
        fetch = lambda s, n: pr(n, title=f"[{old['id']}] x")  # noqa: E731
        # First sight starts from now: history is never backfilled
        assert morpheus.queue_merges(central, cfg, "proj_a", store, fetch=fetch) == []
        clock.advance(10)
        new = store.create_work_order(title="after", description="d")
        store.add_event(new["id"], "pr_merged", {"pr_url": "https://github.com/o/r/pull/2"})
        [item_id] = morpheus.queue_merges(central, cfg, "proj_a", store, fetch=fetch)
        item = central.morpheus_item(item_id)
        assert (item["pr_number"], item["project"], item["state"]) == (2, "proj_a", "queued")
        assert morpheus.queue_merges(central, cfg, "proj_a", store, fetch=fetch) == []
    finally:
        store.close()


def test_a_merge_that_cannot_be_read_is_retried_next_pass(central, cfg, project, clock):
    from jarvis.project_store import ProjectStore

    store = ProjectStore(project)
    try:
        morpheus.queue_merges(central, cfg, "proj_a", store)
        clock.advance(10)
        wo = store.create_work_order(title="x", description="d")
        store.add_event(wo["id"], "pr_merged", {"pr_url": "https://github.com/o/r/pull/5"})

        def broken(s, n):
            raise RuntimeError("gh down")

        assert morpheus.queue_merges(central, cfg, "proj_a", store, fetch=broken) == []
        assert len(morpheus.queue_merges(central, cfg, "proj_a", store,
                                         fetch=lambda s, n: pr(n))) == 1
    finally:
        store.close()


# -- 3. the queue


def test_only_a_waiver_skips_the_queue(central, cfg, clock):
    hinted = queued(central, 1, now=None)
    waived = queued(central, 3, body("None — docs only"))
    missing = queued(central, 4, "## Summary\nx")
    assert (hinted["state"], hinted["script"], hinted["wo_id"]) == ("queued", "", "wo-1234abcd")
    assert hinted["deadline_ts"] == TEST_EPOCH + 3 * DAY
    assert waived["state"] == "validated" and "docs only" in waived["resolution"]
    assert missing["state"] == "queued" and missing["hints"] == ""
    assert queued(central, 1) is None


def test_a_registered_script_starts_monitoring(central, clock):
    item = queued(central, 2)
    moved = morpheus.set_script(central, item["id"], "exit 0", "agent")
    assert (moved["state"], moved["script_source"], moved["next_check_ts"]) == \
        ("monitoring", "agent", TEST_EPOCH)
    with pytest.raises(ValueError):
        morpheus.set_script(central, item["id"], "  ", "agent")


def test_waive_and_requeue(central, cfg, clock):
    item = monitoring(central)
    central.update_morpheus_item(item["id"], state="failed", action="neo:4")
    assert morpheus.waive(central, item["id"], "fine")["state"] == "validated"
    clock.advance(DAY)
    again = morpheus.requeue(central, cfg, item["id"])
    assert (again["state"], again["action"], again["deadline_ts"]) == \
        ("monitoring", "", TEST_EPOCH + DAY + 3 * DAY)


def test_confirm_files_one_fix_and_records_it(central):
    item = monitoring(central)
    filed = []
    with pytest.raises(ValueError, match="only a failed"):
        morpheus.confirm(central, item["id"], "slow", lambda i: "wo-x")
    central.update_morpheus_item(item["id"], state="failed", action="neo:4")
    with pytest.raises(ValueError, match="what needs fixing"):
        morpheus.confirm(central, item["id"], " ", lambda i: "wo-x")
    done, order = morpheus.confirm(central, item["id"], "/cost takes 6.8s",
                                   lambda i: filed.append(i["id"]) or "wo-fix1")
    assert (order, filed, done["state"], done["action"]) == ("wo-fix1", [item["id"]],
                                                             "failed", "neo:4")
    assert done["resolution"].startswith(morpheus.CONFIRMED) and "wo-fix1" in done["resolution"]
    with pytest.raises(ValueError, match="already"):
        morpheus.confirm(central, item["id"], "again", lambda i: "wo-fix2")
    assert "/cost takes 6.8s" in morpheus.fix_description(done, "/cost takes 6.8s")
    assert "morpheus requeue" not in morpheus.fix_description(done, "x")


def test_a_confirmed_item_is_watched_again_once_its_superseding_fix_reaches_production(
        central, cfg):
    item = monitoring(central)
    central.update_morpheus_item(item["id"], state="failed", action="neo:4")
    morpheus.confirm(central, item["id"], "broken", lambda i: "wo-aaaa0001")
    carrier = {"wo-aaaa0001": "wo-bbbb0002"}.get
    follow = lambda wo: carrier(wo) or wo  # noqa: E731
    assert morpheus.requeue_fixed(central, cfg, follow) == []
    queued(central, 8, title="[wo-bbbb0002] the real fix")
    assert morpheus.requeue_fixed(central, cfg, follow) == [item["id"]]
    after = central.morpheus_item(item["id"])
    assert after["state"] == "monitoring" and "wo-bbbb0002" in after["resolution"]
    assert "PR #8" in after["resolution"]
    assert morpheus.requeue_fixed(central, cfg, follow) == []


def test_an_item_settled_by_an_investigation_is_watched_again_once_its_fix_ships(
        central, cfg):
    """#1344: the investigation's filed fix is the item's fix carrier too"""
    item = monitoring(central)
    central.update_morpheus_item(item["id"], state="failed", action="investigation:fo-1nv00001")
    fixes = {"fo-1nv00001": "wo-cccc0003"}
    same = lambda wo: wo  # noqa: E731
    assert morpheus.requeue_fixed(central, cfg, same, lambda inv: "") == []
    assert morpheus.requeue_fixed(central, cfg, same, fixes.get) == [], "not shipped yet"
    queued(central, 9, title="[wo-cccc0003] the fix the investigation filed")
    assert morpheus.requeue_fixed(central, cfg, same, fixes.get) == [item["id"]]
    after = central.morpheus_item(item["id"])
    assert after["state"] == "monitoring" and after["action"] == ""
    assert "wo-cccc0003" in after["resolution"] and "fo-1nv00001" in after["resolution"]
    assert "PR #9" in after["resolution"]


def test_the_fix_loop_stops_at_its_cap_and_asks_the_user(central, monkeypatch):
    from jarvis import ops

    item = queued(central)
    central.update_morpheus_item(item["id"], state="failed")
    candidates = _allowed(monkeypatch)
    monkeypatch.setattr(ops, "find_work_order", lambda wo, p=None: ("proj_a", None, {}))
    cycles = [f"fo-c{i:07d}" for i in range(morpheus.MAX_FIX_CYCLES)]
    monkeypatch.setattr(ops, "morpheus_investigations", lambda p, i: cycles)
    order_id, note = morpheus._investigate(catalog(), central.morpheus_item(item["id"]))
    assert order_id == "" and "the user decides" in note and cycles[-1] in note
    assert candidates == [], "the cap is checked before the steward spends a decision"
    assert any("still fails production validation" in (i["title"] or "")
               for i in central.unacked_inbox())


# -- 4. the mechanical half: the exit code and nothing else


def _run(exit_code, seen=None):
    def run(script, cwd, timeout):
        if seen is not None:
            seen.append(cwd)
        return morpheus.CheckResult(exit_code, f"said {exit_code}")
    return run


@pytest.mark.parametrize("code,state,resolution", [
    (0, "validated", "script exited 0"), (1, "monitoring", ""),
    (75, "monitoring", ""), (None, "monitoring", "")])
def test_the_exit_code_is_the_verdict(central, cfg, tmp_path, code, state, resolution):
    item = monitoring(central)
    moved = morpheus.sweep(central, cfg, tmp_path, now=TEST_EPOCH, run=_run(code))
    after = central.morpheus_item(item["id"])
    assert (after["state"], after["resolution"]) == (state, resolution) and moved[state] == 1
    assert after["checks"] == 1 and after["last_output"] == f"said {code}"
    if state == "monitoring":
        assert after["next_check_ts"] == TEST_EPOCH + cfg.check_every_minutes * 60


def test_failed_needs_two_exit_1_in_a_row(central, cfg, tmp_path):
    item = monitoring(central)
    later = TEST_EPOCH + cfg.check_every_minutes * 60
    morpheus.sweep(central, cfg, tmp_path, now=TEST_EPOCH, run=_run(1))
    morpheus.sweep(central, cfg, tmp_path, now=later, run=_run(1))
    after = central.morpheus_item(item["id"])
    assert (after["state"], after["resolution"]) == ("failed", "script exited 1 twice in a row")
    assert after["checks"] == 2


def test_each_check_is_recorded_as_a_run_and_only_the_last_50_are_kept(central, cfg, tmp_path):
    item = monitoring(central)
    morpheus.sweep(central, cfg, tmp_path, now=TEST_EPOCH, run=_run(75))
    morpheus.sweep(central, cfg, tmp_path, now=TEST_EPOCH + 3600, run=_run(0))
    runs = central.morpheus_runs(item["id"])
    assert [(r["exit"], r["kind"], r["state_after"], r["output"]) for r in runs] == [
        (0, "check", "validated", "said 0"), (75, "check", "monitoring", "said 75")]
    for n in range(60):
        central.add_morpheus_run(item["id"], exit=75, output="", now=TEST_EPOCH + 7200 + n)
    runs = central.morpheus_runs(item["id"], limit=1000)
    assert len(runs) == 50 and runs[0]["ts"] == TEST_EPOCH + 7259


def test_an_exit_1_the_next_check_does_not_repeat_is_not_a_failure(central, cfg, tmp_path):
    item = monitoring(central)
    for n, code in enumerate((1, 75, 1)):
        morpheus.sweep(central, cfg, tmp_path, now=TEST_EPOCH + n * cfg.check_every_minutes * 60,
                       run=_run(code))
    assert central.morpheus_item(item["id"])["state"] == "monitoring"


def test_an_unconfirmed_exit_1_times_out_saying_so(central, cfg, tmp_path):
    item = monitoring(central)
    morpheus.sweep(central, cfg, tmp_path, now=TEST_EPOCH + 3 * DAY + 1, run=_run(1))
    after = central.morpheus_item(item["id"])
    assert (after["state"], after["resolution"]) == \
        ("timedout", "one exit 1 within 3 day(s), never confirmed by a second")


def test_show_says_when_the_last_check_ran_and_on_which_version(central, cfg, tmp_path,
                                                                 capsys):
    from jarvis.cli import main

    item = monitoring(central)
    morpheus.check(central, cfg, item, tmp_path, now=TEST_EPOCH, run=_run(1),
                   version=lambda where: "jarvis-0.12.31")
    assert central.morpheus_item(item["id"])["last_check_version"] == "jarvis-0.12.31"
    capsys.readouterr()
    assert main(["morpheus", "show", item["id"]]) == 0
    out = capsys.readouterr().out
    assert "first release with this PR" in out and " on jarvis-0.12.31" in out


def test_each_script_runs_in_its_own_project(central, cfg, tmp_path):
    monitoring(central, n=1, project="a")
    monitoring(central, n=2, project="b")
    seen: list[Path] = []
    morpheus.sweep(central, cfg, lambda item: tmp_path / item["project"], now=TEST_EPOCH,
                   run=_run(75, seen))
    assert sorted(seen) == [tmp_path / "a", tmp_path / "b"]


def test_a_script_is_not_run_before_it_is_due(central, cfg, tmp_path):
    monitoring(central)
    morpheus.sweep(central, cfg, tmp_path, now=TEST_EPOCH, run=_run(75))
    ran = []
    morpheus.sweep(central, cfg, tmp_path, now=TEST_EPOCH + 60,
                   run=lambda *a: ran.append(1) or morpheus.CheckResult(0, ""))
    assert ran == []


def test_not_yet_past_the_deadline_is_timedout(central, cfg, tmp_path):
    item = monitoring(central)
    morpheus.sweep(central, cfg, tmp_path, now=TEST_EPOCH + 3 * DAY + 1, run=_run(75))
    after = central.morpheus_item(item["id"])
    assert after["state"] == "timedout"
    assert after["resolution"] == "no exit 0 or 1 within 3 day(s) (last exit None)"


# -- an exit 75 names why (monitoring-orders §9)


def _said(output: str, code: int = 75):
    return lambda script, cwd, timeout: morpheus.CheckResult(code, output)


@pytest.mark.parametrize("output,reason", [
    ("UNKNOWN x\nREASON not_exercised", "not_exercised"),
    ("REASON not_deployed\nsome stderr after it", "not_deployed"),
    ("REASON unreadable\nREASON ambiguous", "ambiguous"),
    ("REASON because", "ambiguous"), ("UNKNOWN x", "ambiguous"), ("", "ambiguous")])
def test_check_stores_the_reason_and_a_missing_one_means_ambiguous(central, cfg, tmp_path,
                                                                   output, reason):
    item = monitoring(central)
    morpheus.check(central, cfg, item, tmp_path, now=TEST_EPOCH, run=_said(output))
    assert central.morpheus_item(item["id"])["last_reason"] == reason


@pytest.mark.parametrize("code", [0, 1, None])
def test_only_an_exit_75_has_a_reason(central, cfg, tmp_path, code):
    item = monitoring(central)
    morpheus.check(central, cfg, item, tmp_path, now=TEST_EPOCH,
                   run=_said("REASON not_exercised", code))
    assert central.morpheus_item(item["id"])["last_reason"] == ""


def test_a_not_exercised_timeout_settles_unverified_with_no_model_call(central, cfg, tmp_path):
    item = monitoring(central)
    moved = morpheus.sweep(central, cfg, tmp_path, now=TEST_EPOCH + 3 * DAY + 1,
                           run=_said("UNKNOWN no event yet\nREASON not_exercised"))
    after = central.morpheus_item(item["id"])
    assert after["state"] == "unverified" and moved["unverified"] == 1
    assert after["next_check_ts"] is None and "never exercised" in after["resolution"]
    assert morpheus.owed_actions(central) == []

    def no_model(*a, **kw):
        raise AssertionError("a settled order made a model call")
    assert morpheus.act(central, catalog(), after, cwd=tmp_path, call=no_model,
                        ask_neo=no_model, investigate=no_model) == ""


def test_unverified_is_terminal_and_never_counted_as_passed():
    from jarvis.release_view import validation_of

    assert "unverified" in morpheus.TERMINAL_STATES
    assert "unverified" not in morpheus.ACTION_STATES
    assert validation_of({"state": "unverified"}) == "unwatched"


@pytest.mark.parametrize("earlier,output", [
    ("FAIL the symptom is back\nREASON not_exercised", "REASON not_exercised"),
    ("said 1", "REASON not_exercised"),
    ("UNKNOWN x", "FAIL the symptom is back\nREASON not_exercised"),
    ("x" * morpheus.OUTPUT_TAIL, "REASON not_exercised")])
def test_a_fail_line_anywhere_in_the_watch_keeps_the_judge_route(central, cfg, tmp_path,
                                                                 earlier, output):
    item = monitoring(central)
    code = 1 if earlier == "said 1" else 75
    morpheus.check(central, cfg, item, tmp_path, now=TEST_EPOCH, run=_said(earlier, code))
    morpheus.check(central, cfg, central.morpheus_item(item["id"]), tmp_path,
                   now=TEST_EPOCH + 3 * DAY + 1, run=_said(output))
    assert central.morpheus_item(item["id"])["state"] == "timedout"
    assert [i["id"] for i in morpheus.owed_actions(central)] == [item["id"]]


@pytest.mark.parametrize("output", ["REASON unreadable", "REASON ambiguous", "no reason at all"])
def test_unreadable_and_ambiguous_keep_the_judge_route(central, cfg, tmp_path, output):
    item = monitoring(central)
    morpheus.sweep(central, cfg, tmp_path, now=TEST_EPOCH + 3 * DAY + 1, run=_said(output))
    assert central.morpheus_item(item["id"])["state"] == "timedout"
    assert [i["id"] for i in morpheus.owed_actions(central)] == [item["id"]]


def test_not_deployed_holds_the_deadline_and_the_window_starts_once_deployed(central, cfg,
                                                                              tmp_path):
    item = monitoring(central)
    held_at = TEST_EPOCH + 5 * DAY
    morpheus.sweep(central, cfg, tmp_path, now=held_at, run=_said("REASON not_deployed"))
    after = central.morpheus_item(item["id"])
    assert after["state"] == "monitoring" and after["held_since_ts"] == held_at
    assert after["deadline_ts"] == held_at + 3 * DAY
    # Deployed now: the full window remains, then the ordinary not_exercised settle
    morpheus.check(central, cfg, after, tmp_path, now=held_at + DAY,
                   run=_said("REASON not_exercised"))
    after = central.morpheus_item(item["id"])
    assert after["state"] == "monitoring" and after["deadline_ts"] == held_at + 3 * DAY
    morpheus.check(central, cfg, after, tmp_path, now=held_at + 3 * DAY,
                   run=_said("REASON not_exercised"))
    assert central.morpheus_item(item["id"])["state"] == "unverified"


def test_not_deployed_stops_holding_at_the_hard_cap_and_goes_to_the_judge(central, cfg, tmp_path):
    item = monitoring(central)
    since = TEST_EPOCH + DAY
    for day in range(0, 14):
        morpheus.check(central, cfg, central.morpheus_item(item["id"]), tmp_path,
                       now=since + day * DAY, run=_said("REASON not_deployed"))
    after = central.morpheus_item(item["id"])
    assert after["state"] == "monitoring" and after["deadline_ts"] == since + 14 * DAY
    assert after["held_since_ts"] == since
    morpheus.check(central, cfg, after, tmp_path, now=since + 14 * DAY,
                   run=_said("REASON not_deployed"))
    after = central.morpheus_item(item["id"])
    assert after["state"] == "timedout" and "not deployed after 14 day(s)" in after["resolution"]
    assert [i["id"] for i in morpheus.owed_actions(central)] == [item["id"]]


def test_a_requeue_gives_a_fresh_watch(central, cfg, tmp_path):
    item = monitoring(central)
    morpheus.check(central, cfg, item, tmp_path, now=TEST_EPOCH,
                   run=_said("FAIL x\nREASON not_deployed"))
    morpheus.requeue(central, cfg, item["id"])
    after = central.morpheus_item(item["id"])
    assert (after["last_reason"], after["saw_fail"], after["held_since_ts"]) == ("", 0, None)


def test_arming_asks_for_a_probe_first_and_a_reason_and_the_judge_inherits_the_rule():
    for text in (morpheus.ARM_CONTRACT, morpheus.JUDGE_CONTRACT):
        assert "REASON" in text and "not_exercised" in text and "probe" in text.lower()
    assert '"trigger"' in morpheus.ARM_CONTRACT


@pytest.mark.parametrize("said,stored", [("probe", "probe"), ("Ambient", "ambient"),
                                         ("whenever", ""), (None, "")])
def test_arming_records_the_trigger(central, tmp_path, said, stored):
    item = queued(central, 3)
    reply = {"script": "exit 75", "notes": "n", "trigger": said}
    morpheus.arm(central, catalog(), tmp_path, now=TEST_EPOCH, call=FakeCall(reply=reply))
    after = central.morpheus_item(item["id"])
    assert (after["state"], after["trigger_kind"]) == ("monitoring", stored)


def test_a_queued_pr_that_never_got_a_script_times_out_too(central, cfg, tmp_path):
    item = queued(central, 2)
    morpheus.sweep(central, cfg, tmp_path, now=TEST_EPOCH + 3 * DAY + 1, run=_run(0))
    after = central.morpheus_item(item["id"])
    assert (after["state"], after["resolution"]) == \
        ("timedout", "no script was registered within 3 day(s)")


def test_run_script_reads_the_real_exit_code_and_output(tmp_path):
    assert morpheus.run_script("echo hi; exit 1", tmp_path, 10) == \
        morpheus.CheckResult(1, "hi")
    timed = morpheus.run_script("sleep 5", tmp_path, 1)
    assert timed.exit is None and "timed out" in timed.output


# -- 5. failed and timed out hand off: Morpheus never diagnoses


PROD = Path("/prod")


def CONFIRM(prompt, system_prompt=None, **kw):
    """A judge that confirms the failure"""
    return claude_cli.HeadlessResult(
        text=json.dumps({"verdict": "confirm", "reason": "the job errors in the log"}),
        usage={"input_tokens": 1, "output_tokens": 1}, session_id="s-c", model="sonnet")


def _failed(central) -> dict:
    item = monitoring(central)
    central.update_morpheus_item(item["id"], state="failed", resolution="script exited 1",
                                 last_exit=1)
    return central.morpheus_item(item["id"])


def test_failed_with_a_work_order_files_an_investigation_once(central):
    item = _failed(central)
    asked = []
    action = morpheus.act(central, catalog(), item, cwd=PROD, call=CONFIRM,
                          investigate=lambda c, i: ("fo-inv00001", ""),
                          ask_neo=lambda *a: asked.append(1) or 9)
    assert action == "investigation:fo-inv00001" and asked == []
    assert central.morpheus_item(item["id"])["action"] == action
    assert morpheus.owed_actions(central) == []
    [row] = central.conn.execute("SELECT project, title, body FROM inbox").fetchall()
    assert row["project"] == "proj_a"
    assert row["title"] == "PR #7 failed production validation (script exited 1)"
    assert "fo-inv00001 is finding out why" in row["body"]


def test_the_hand_off_states_only_facts(central):
    why = morpheus._why(_failed(central))
    assert "has NOT diagnosed anything" in why and "script exited 1" in why
    assert "broken" not in why.lower()


def test_an_investigation_that_cannot_be_filed_asks_neo_instead(central):
    notes = []
    action = morpheus.act(central, catalog(), _failed(central), cwd=PROD, call=CONFIRM,
                          investigate=lambda c, i: ("", "the steward said no"),
                          ask_neo=lambda c, i, note: notes.append(note) or 9)
    assert action == "neo:9" and notes == ["the steward said no"]


def test_timedout_asks_neo_by_default_and_the_route_is_configurable(central):
    item = queued(central)
    central.update_morpheus_item(item["id"], state="timedout")
    filed = []
    assert morpheus.act(central, catalog(), central.morpheus_item(item["id"]),
                        cwd=PROD, call=CONFIRM,
                        investigate=lambda c, i: filed.append(1) or ("x", ""),
                        ask_neo=lambda *a: 3) == "neo:3"
    assert filed == []
    central.update_morpheus_item(item["id"], action="")
    assert morpheus.act(central, catalog(on_timeout="investigate"),
                        central.morpheus_item(item["id"]), cwd=PROD, call=CONFIRM,
                        investigate=lambda c, i: ("fo-x", ""),
                        ask_neo=lambda *a: 3) == "investigation:fo-x"


def _allowed(monkeypatch, allowed: bool = True) -> list:
    from jarvis import steward

    seen = []

    def gate(c, **kw):
        seen.append(c)
        return steward.Decision(outcome="dispatch" if allowed else "refuse", rule="test",
                                reason="" if allowed else "no", severity="critical",
                                claimed_severity="critical", generation=0, mode="test",
                                allowed=allowed)

    monkeypatch.setattr(steward, "gate", gate)
    return seen


def test_no_work_order_files_an_improvement_order_on_the_pr(central, monkeypatch):
    from jarvis import ops

    item = queued(central, title="untitled")
    candidates = _allowed(monkeypatch)
    filed = {}

    def create(project, title, description="", refs=(), origin="jarvis", **kw):
        filed.update(project=project, title=title, description=description,
                     refs=list(refs), origin=origin)
        return {"id": "io-abcd1234"}

    monkeypatch.setattr(ops, "create_improvement_order", create)
    assert morpheus._investigate(catalog(), item) == ("io-abcd1234", "")
    assert filed["project"] == "proj_a" and filed["refs"] == [item["pr_url"]]
    assert filed["origin"] == "morpheus" and "has NOT diagnosed anything" in filed["description"]
    assert [c.origin_kind for c in candidates] == ["morpheus"]


def test_an_improvement_order_the_steward_refuses_asks_neo(central, monkeypatch):
    item = queued(central, title="untitled")
    _allowed(monkeypatch, allowed=False)
    order_id, note = morpheus._investigate(catalog(), item)
    assert order_id == "" and "the steward did not dispatch it" in note


def test_an_improvement_order_is_recorded_as_one_not_as_an_investigation(central):
    assert morpheus.act(central, catalog(), _failed(central), cwd=PROD, call=CONFIRM,
                        investigate=lambda c, i: ("io-abcd1234", ""),
                        ask_neo=lambda *a: 9) == "improvement:io-abcd1234"


def test_the_neo_question_is_a_validation_question_naming_the_item(central):
    from jarvis.neo_store import NeoStore

    item = queued(central)
    central.update_morpheus_item(item["id"], state="timedout", resolution="no verdict")
    qid = morpheus._ask_neo(catalog(), central.morpheus_item(item["id"]), "")
    neo = NeoStore()
    try:
        q = neo.get(qid)
    finally:
        neo.close()
    assert (q["kind"], q["wo_id"], q["project"]) == ("validation", "", "proj_a")
    assert morpheus.item_of_question(q) == item["id"]
    assert f"jarvis morpheus waive {item['id']}" in q["question"]
    assert '"action": "requeue"' in q["question"]


def test_morpheus_detail_gathers_runs_questions_retries_and_handoff(central):
    from jarvis import ops

    item = queued(central)
    other = queued(central, 8)
    central.update_morpheus_item(item["id"], state="timedout", resolution="no verdict")
    qid = morpheus._ask_neo(catalog(), central.morpheus_item(item["id"]), "")
    morpheus._ask_neo(catalog(), central.morpheus_item(other["id"]), "")
    central.claim_morpheus_action(item["id"], f"neo:{qid}")
    detail = ops.morpheus_detail(item["id"])
    assert set(detail) == {"item", "runs", "questions", "next_check_ts", "retries", "handoff",
                           "investigations"}
    assert detail["runs"] == [] and detail["item"]["id"] == item["id"]
    assert [(q["id"], q["kind"]) for q in detail["questions"]] == [(qid, "validation")]
    assert set(detail["questions"][0]) == {"id", "status", "kind", "question", "answer", "ts"}
    assert detail["retries"] == {"checks": 0, "auto_requeues": 0,
                                 "max_auto_requeues": morpheus.MAX_AUTO_REQUEUES}
    assert detail["handoff"] == {"kind": "neo", "ref": str(qid), "why": ""}
    central.update_morpheus_item(other["id"], action="none:waived by the user")
    assert ops.morpheus_detail(other["id"])["handoff"] == {
        "kind": "none", "ref": "", "why": "waived by the user"}
    with pytest.raises(ops.OpsError):
        ops.morpheus_detail("mv-nope")


def test_an_item_that_spent_its_requeue_is_not_offered_one(central):
    item = queued(central)
    central.update_morpheus_item(item["id"], state="failed", resolution="script exited 1")
    central.claim_morpheus_auto_requeue(item["id"], morpheus.MAX_AUTO_REQUEUES)
    qid = morpheus._ask_neo(catalog(), central.morpheus_item(item["id"]), "")
    from jarvis.neo_store import NeoStore

    neo = NeoStore()
    try:
        question = neo.get(qid)["question"]
    finally:
        neo.close()
    assert "already spent its one automatic requeue" in question
    assert f"jarvis morpheus requeue {item['id']}" in question


def _verdict(central, item_id: str, **verdict) -> None:
    q = {"id": 5, "project": "proj_a", "context": json.dumps({"morpheus_item": item_id})}
    morpheus.record_neo_verdict(central, MorpheusConfig(), q,
                                {"escalate": False, "answer": "rare path", "reason": "",
                                 **verdict}, "http://x/neo/question/5")


@pytest.mark.parametrize("escalate,level", [(True, "warning"), (False, "info")])
def test_neo_s_verdict_is_recorded_and_reaches_the_inbox(central, escalate, level):
    item = queued(central)
    central.update_morpheus_item(item["id"], state="timedout", resolution="no verdict")
    _verdict(central, item["id"], escalate=escalate)
    after = central.morpheus_item(item["id"])
    assert after["state"] == "timedout" and after["resolution"].endswith("Neo: rare path")
    rows = central.conn.execute("SELECT level, title FROM inbox").fetchall()
    assert [(r["level"], "PR #7" in r["title"]) for r in rows] == [(level, True)]


def test_neo_s_requeue_verdict_puts_the_item_back_on_the_watch_list(central):
    item = monitoring(central, script="exit 1")
    central.update_morpheus_item(item["id"], state="failed", resolution="script exited 1",
                                 next_check_ts=None, action="neo:5")
    _verdict(central, item["id"], action="requeue")
    after = central.morpheus_item(item["id"])
    assert (after["state"], after["action"], after["auto_requeues"]) == ("monitoring", "", 1)
    assert after["resolution"].startswith("re-watched by Neo:")
    assert after["next_check_ts"] is not None


def test_the_second_automatic_requeue_is_refused(central):
    item = monitoring(central, script="exit 1")
    central.update_morpheus_item(item["id"], state="failed", resolution="script exited 1")
    _verdict(central, item["id"], action="requeue")
    central.update_morpheus_item(item["id"], state="failed", resolution="script exited 1")
    _verdict(central, item["id"], action="requeue")
    after = central.morpheus_item(item["id"])
    assert (after["state"], after["auto_requeues"]) == ("failed", 1)


@pytest.mark.parametrize("verdict", [{"escalate": True, "action": "requeue"},
                                     {"action": "waive"}, {"action": ""}])
def test_only_an_answered_requeue_acts_on_the_item(central, verdict):
    item = monitoring(central, script="exit 1")
    central.update_morpheus_item(item["id"], state="failed", resolution="script exited 1")
    _verdict(central, item["id"], **verdict)
    after = central.morpheus_item(item["id"])
    assert (after["state"], after["auto_requeues"]) == ("failed", 0)


def test_a_verdict_never_pulls_a_waived_item_back_into_validation(central):
    item = monitoring(central, script="exit 1")
    morpheus.waive(central, item["id"], "shipped elsewhere")
    _verdict(central, item["id"], action="requeue")
    after = central.morpheus_item(item["id"])
    assert (after["state"], after["auto_requeues"]) == ("validated", 0)


def test_neo_s_rewrite_verdict_sends_the_script_back_to_morpheus(central):
    item = monitoring(central, script="echo old; exit 1")
    central.update_morpheus_item(item["id"], state="failed", resolution="script exited 1",
                                 last_exit=1, next_check_ts=None, action="neo:5")
    _verdict(central, item["id"], action="rewrite", answer="keys on resolution; use state")
    after = central.morpheus_item(item["id"])
    assert (after["state"], after["script"], after["last_exit"], after["action"],
            after["script_rewrites"]) == ("queued", "", None, "", 1)
    assert "keys on resolution; use state" in after["rewrite_brief"]
    assert "echo old; exit 1" in after["rewrite_brief"]
    assert [i["id"] for i in morpheus.owed_arming(central)] == [item["id"]]
    assert "A REWRITE" in morpheus._arm_prompt(after)
    assert morpheus.set_script(central, item["id"], "exit 0", "agent")["state"] == "monitoring"


def test_the_second_automatic_rewrite_is_refused_and_neo_is_told(central):
    item = monitoring(central, script="exit 1")
    for _ in range(2):
        central.update_morpheus_item(item["id"], state="failed", script="exit 1")
        _verdict(central, item["id"], action="rewrite")
    after = central.morpheus_item(item["id"])
    assert (after["state"], after["script_rewrites"]) == ("failed", 1)
    qid = morpheus._ask_neo(catalog(), after, "")
    from jarvis.neo_store import NeoStore

    neo = NeoStore()
    try:
        question = neo.get(qid)["question"]
    finally:
        neo.close()
    assert f"jarvis morpheus rewrite {item['id']}" in question


def test_a_validation_question_cannot_be_answered_like_a_worker_question(central):
    from jarvis import ops

    item = queued(central)
    qid = morpheus._ask_neo(catalog(), item, "")
    with pytest.raises(ops.OpsError, match=f"jarvis morpheus waive {item['id']}"):
        ops.neo_answer_escalated(qid, "looks fine")


def _escalated(central) -> tuple[dict, int]:
    """A failed item whose Neo question came back to the user"""
    from jarvis.neo_store import NeoStore

    item = _failed(central)
    qid = morpheus._ask_neo(catalog(), item, "")
    central.claim_morpheus_action(item["id"], f"neo:{qid}")
    neo = NeoStore()
    try:
        neo.mark(qid, "escalated", reason="needs you")
    finally:
        neo.close()
    return central.morpheus_item(item["id"]), qid


def _status(qid: int) -> str:
    from jarvis.neo_store import NeoStore

    neo = NeoStore()
    try:
        return neo.get(qid)["status"]
    finally:
        neo.close()


@pytest.mark.parametrize("decide", ["waive", "requeue"])
def test_deciding_the_item_closes_its_question(central, decide):
    from jarvis import ops

    item, qid = _escalated(central)
    if decide == "waive":
        ops.morpheus_waive(item["id"], "the meter reads fine")
    else:
        ops.morpheus_requeue(item["id"])
    assert _status(qid) == "answered"


def test_a_question_whose_item_was_already_decided_is_swept(central, tmp_path, monkeypatch):
    from jarvis import invariants, ops
    from jarvis.project_store import ProjectStore

    item, qid = _escalated(central)
    store = ProjectStore(tmp_path / "proj_a")
    monkeypatch.setattr(ops, "registered_project_paths", lambda: {"proj_a": store.project_path})
    try:
        assert list(invariants.check_neo_escalations_are_live(store)) == []
        morpheus.requeue(central, MorpheusConfig(), item["id"])  # decided without ops
        [v] = list(invariants.check_neo_escalations_are_live(store))
    finally:
        store.close()
    assert (v.invariant, v.context["question_id"]) == ("INV-NEO-ESCALATION-STALE", qid)
    assert _status(qid) == "answered"


def test_the_neo_tab_decides_a_validation_question_on_its_item(central):
    from fastapi.testclient import TestClient

    from jarvis.ui.app import create_app

    item, qid = _escalated(central)
    client = TestClient(create_app())
    page = client.get("/neo")
    assert f'action="/mo/{item["id"]}/waive"' in page.text
    assert f'action="/mo/{item["id"]}/requeue"' in page.text
    assert "Send answer to worker" not in page.text

    r = client.post(f"/mo/{item['id']}/waive", follow_redirects=False,
                    data={"reason": " ", "next": "/neo#tab-escalated"})
    assert "error=" in r.headers["location"] and _status(qid) == "escalated"
    r = client.post(f"/mo/{item['id']}/waive", follow_redirects=False,
                    data={"reason": "works", "next": "/neo#tab-escalated"})
    assert r.headers["location"] == "/neo#tab-escalated"
    assert central.morpheus_item(item["id"])["state"] == "validated"
    assert _status(qid) == "answered"


# -- 6. arming in a short session (docs/superpowers/specs/2026-11-01-monitoring-orders.md §3)


class FakeCall:
    """Stands in for claude_cli.run_headless_result: one reply per call, or an error"""

    def __init__(self, reply: object = None, error: BaseException | None = None):
        self.calls: list[dict] = []
        self.reply = {"script": "exit 0", "notes": "checks it"} if reply is None else reply
        self.error = error

    def __call__(self, prompt, system_prompt=None, **kw):
        self.calls.append({"prompt": prompt, "system_prompt": system_prompt, **kw})
        if self.error is not None:
            raise self.error
        text = self.reply if isinstance(self.reply, str) else json.dumps(self.reply)
        return claude_cli.HeadlessResult(
            text=text, usage={"input_tokens": 10, "output_tokens": 5},
            session_id=f"s-{len(self.calls)}", model="sonnet")


def sigkilled(rc: int = 143) -> "claude_cli.KilledError":
    return claude_cli.KilledError(claude_cli.Killed(
        message=f"claude -p --output-format json... was killed by a signal (rc={rc}) "
                "and wrote nothing", rc=rc))


def refused(reset_at: float | None) -> "claude_cli.UsageLimitError":
    return claude_cli.UsageLimitError(claude_cli.UsageLimit(
        message="You've hit your session limit · resets 6:20am (America/Los_Angeles)",
        reset_at=reset_at))


def test_no_queued_order_means_no_session(central, tmp_path):
    call = FakeCall()
    monitoring(central)
    assert morpheus.arm(central, catalog(), tmp_path, now=TEST_EPOCH, call=call) == \
        {"armed": 0, "refused": 0, "failed": 0, "killed": 0}
    assert call.calls == []


def test_a_release_of_three_beside_fifteen_monitoring_makes_exactly_three_calls(central,
                                                                                tmp_path):
    watched = {}
    for n in range(100, 115):
        watched[monitoring(central, script=f"exit 75 # {n}", n=n)["id"]] = f"exit 75 # {n}"
    new = [queued(central, n, body(f"Watch hint {n}."), project="proj_b",
                  now=TEST_EPOCH + n) for n in (1, 2, 3)]
    call = FakeCall()
    dirs = {"proj_b": tmp_path / "proj_b"}
    assert morpheus.arm(central, catalog(), lambda i: dirs[i["project"]], now=TEST_EPOCH + 9,
                        call=call)["armed"] == 3
    assert len(call.calls) == 3
    # Oldest first, each a fresh call that keeps its transcript, built only from its row
    for c, item in zip(call.calls, new):
        assert c["persist"] is True and c["tools"] == morpheus.SESSION_TOOLS
        assert c["cwd"] == tmp_path / "proj_b" and c["model"] == "sonnet"
        assert c["system_prompt"] == morpheus.MORPHEUS_PERSONA
        assert item["id"] in c["prompt"] and item["pr_url"] in c["prompt"]
        assert f"Watch hint {item['pr_number']}." in c["prompt"]
        assert "exits 1 ONLY" in c["prompt"] and "exits 75" in c["prompt"]
        assert not any(o["id"] in c["prompt"] for o in new if o is not item)
        assert "resume" not in c and "session_id" not in c
    for item_id, script in watched.items():
        assert central.morpheus_item(item_id)["script"] == script
    for n, item in enumerate(new, 1):
        after = central.morpheus_item(item["id"])
        assert (after["state"], after["script"], after["script_source"], after["attempts"],
                after["last_session_id"]) == ("monitoring", "exit 0", "agent", 0, f"s-{n}")
    rows = central.conn.execute(
        "SELECT kind, label, wo_id, project FROM agent_calls ORDER BY id").fetchall()
    assert [tuple(r) for r in rows] == [("morpheus", "arm", i["id"], "proj_b") for i in new]


@pytest.mark.parametrize("call", [FakeCall(reply="I wrote it to /tmp, done."),
                                  FakeCall(error=claude_cli.ClaudeCliError("boom"))],
                         ids=["unparseable", "cli-error"])
def test_a_session_with_no_answer_spends_one_attempt_and_changes_no_state(central, tmp_path,
                                                                          call):
    item = queued(central, 2)
    assert morpheus.arm(central, catalog(), tmp_path, now=TEST_EPOCH, call=call)["failed"] == 1
    after = central.morpheus_item(item["id"])
    assert (after["state"], after["script"], after["attempts"], after["last_attempt_ts"]) == \
        ("queued", "", 1, TEST_EPOCH)
    # Not again until REOFFER_SECONDS, and never past MAX_OFFERS
    morpheus.arm(central, catalog(), tmp_path, now=TEST_EPOCH + 60, call=call)
    assert len(call.calls) == 1
    for n in range(1, morpheus.MAX_OFFERS + 2):
        morpheus.arm(central, catalog(), tmp_path, call=call,
                     now=TEST_EPOCH + n * (morpheus.REOFFER_SECONDS + 1))
    assert len(call.calls) == morpheus.MAX_OFFERS
    assert central.morpheus_item(item["id"])["attempts"] == morpheus.MAX_OFFERS
    assert morpheus.activity(central, now=TEST_EPOCH + 10 * DAY)["owed_arm"] == 0


def test_a_refusal_spends_an_attempt_and_says_why(central, tmp_path):
    item = queued(central, 2)
    call = FakeCall(reply={"refuse": "the hints name no production signal"})
    assert morpheus.arm(central, catalog(), tmp_path, now=TEST_EPOCH, call=call)["refused"] == 1
    after = central.morpheus_item(item["id"])
    assert (after["state"], after["attempts"]) == ("queued", 1)
    assert "the hints name no production signal" in after["resolution"]


def test_the_usage_window_holds_every_order_and_costs_none_an_attempt(central, tmp_path):
    first, second = queued(central, 2, now=TEST_EPOCH), queued(central, 3, now=TEST_EPOCH + 1)
    reset_at = TEST_EPOCH + 4000
    call = FakeCall(error=refused(reset_at))
    assert morpheus.arm(central, catalog(), tmp_path, now=TEST_EPOCH, call=call) == \
        {"armed": 0, "refused": 0, "failed": 0, "killed": 0, "usage_held": 1}
    # The second order was never tried: the window is the account's
    assert len(call.calls) == 1
    assert all(central.morpheus_item(i["id"])["attempts"] == 0 for i in (first, second))
    assert morpheus.usage_hold_until(central, TEST_EPOCH) == reset_at
    act = morpheus.activity(central, now=TEST_EPOCH + 60)
    assert (act["state"], act["usage_hold_until"]) == ("usage_held", reset_at)
    call.error = None
    assert morpheus.arm(central, catalog(), tmp_path, now=reset_at - 1, call=call) == \
        {"usage_held": 1}
    assert len(call.calls) == 1
    assert morpheus.arm(central, catalog(), tmp_path, now=reset_at, call=call)["armed"] == 2


def test_a_refusal_that_named_no_reset_holds_for_the_fallback_delay(central, tmp_path):
    from jarvis.worker_session import RATE_LIMIT_FALLBACK_DELAY

    queued(central, 2)
    morpheus.arm(central, catalog(), tmp_path, now=TEST_EPOCH, call=FakeCall(error=refused(None)))
    hold = morpheus.usage_hold_until(central, TEST_EPOCH)
    assert hold is not None and hold >= TEST_EPOCH + RATE_LIMIT_FALLBACK_DELAY - 5


def test_kills_cost_no_attempt_until_max_kills_in_a_row_cost_one(central, tmp_path):
    """kn-1e43fe1a: the exemption has its own bound, so a deterministic kill still ends"""
    item = queued(central, 2)
    killer = FakeCall(error=sigkilled())
    for n in range(morpheus.MAX_KILLS - 1):
        assert morpheus.arm(central, catalog(), tmp_path, now=TEST_EPOCH + n,
                            call=killer)["killed"] == 1
    after = central.morpheus_item(item["id"])
    assert (after["kills"], after["attempts"]) == (morpheus.MAX_KILLS - 1, 0)
    morpheus.arm(central, catalog(), tmp_path, now=TEST_EPOCH + 10, call=killer)
    after = central.morpheus_item(item["id"])
    assert (after["kills"], after["attempts"], after["last_attempt_ts"]) == \
        (0, 1, TEST_EPOCH + 10)
    # Spent: no retry until REOFFER_SECONDS
    morpheus.arm(central, catalog(), tmp_path, now=TEST_EPOCH + 20, call=killer)
    assert len(killer.calls) == morpheus.MAX_KILLS


def test_an_answered_session_resets_the_kill_count(central, tmp_path):
    item = queued(central, 2)
    morpheus.arm(central, catalog(), tmp_path, now=TEST_EPOCH, call=FakeCall(error=sigkilled()))
    assert central.morpheus_item(item["id"])["kills"] == 1
    morpheus.arm(central, catalog(), tmp_path, now=TEST_EPOCH + 1, call=FakeCall())
    after = central.morpheus_item(item["id"])
    assert (after["kills"], after["state"]) == (0, "monitoring")


@pytest.mark.parametrize("pause", ["pin", "fleet"])
def test_a_pause_stops_arming_and_nothing_else(central, tmp_path, pause):
    from jarvis import fleet

    queued(central, 2)
    if pause == "pin":
        morpheus.set_pause(central, "paused", "quiet", now=TEST_EPOCH)
    else:
        fleet.pause(central, reason="spend")
    call = FakeCall()
    assert morpheus.arm(central, catalog(), tmp_path, now=TEST_EPOCH, call=call) == {"paused": 1}
    assert call.calls == []
    assert morpheus.activity(central, now=TEST_EPOCH)["state"] == "paused"
    if pause == "fleet":
        morpheus.set_pause(central, "running", "validate the release", now=TEST_EPOCH)
        assert morpheus.arm(central, catalog(), tmp_path, now=TEST_EPOCH,
                            call=call)["armed"] == 1


def test_a_user_script_returned_unchanged_stays_the_users(central, tmp_path):
    item = queued(central, 2)
    central.update_morpheus_item(item["id"], script="exit 0", script_source="user")
    call = FakeCall(reply={"script": "exit 0\n", "notes": "fine"})
    morpheus.arm(central, catalog(), tmp_path, now=TEST_EPOCH, call=call)
    assert "The user wrote this script" in call.calls[0]["prompt"]
    assert central.morpheus_item(item["id"])["script_source"] == "user"
    other = queued(central, 3)
    central.update_morpheus_item(other["id"], script="exit 1", script_source="user")
    morpheus.arm(central, catalog(), tmp_path, now=TEST_EPOCH, call=call)
    assert central.morpheus_item(other["id"])["script_source"] == "agent"


def test_a_rewrite_requeues_with_a_fresh_count_and_keeps_a_users_script(central, cfg, tmp_path):
    item = monitoring(central, script="exit 1")
    central.update_morpheus_item(item["id"], script_source="user", state="failed", attempts=2,
                                 kills=1, last_attempt_ts=TEST_EPOCH)
    after = morpheus.rewrite(central, cfg, item["id"], "reads the wrong field")
    assert (after["state"], after["script"], after["script_source"], after["attempts"],
            after["kills"], after["last_attempt_ts"]) == ("queued", "exit 1", "user", 0, 0, None)
    call = FakeCall(reply={"script": "exit 0", "notes": "fixed"})
    morpheus.arm(central, catalog(), tmp_path, now=TEST_EPOCH, call=call)
    assert "A REWRITE" in call.calls[0]["prompt"] and "reads the wrong field" in \
        call.calls[0]["prompt"]
    assert central.morpheus_item(item["id"])["state"] == "monitoring"


def test_the_first_pass_drops_the_long_lived_sessions_record(central, tmp_path):
    central.set_state("morpheus.session", json.dumps({"turns": 9}))
    morpheus.arm(central, catalog(), tmp_path, now=TEST_EPOCH, call=FakeCall())
    assert not central.get_state("morpheus.session")


def test_an_unknown_purpose_is_refused_before_any_call(central, tmp_path):
    call = FakeCall()
    with pytest.raises(ValueError, match="no session purpose"):
        morpheus.run_session(central, catalog(), queued(central, 2), purpose="nope",
                             cwd=tmp_path, call=call)
    assert call.calls == []


def test_the_long_lived_session_is_gone():
    """Grep pin: nothing resumes a Morpheus session any more"""
    assert not hasattr(claude_cli, "run_session_turn")
    for name in ("SESSION_KEY", "wake", "session_state", "wanting_scripts", "build_prompt",
                 "kill_hold", "KILL_HOLD_SECONDS"):
        assert not hasattr(morpheus, name), name
    src = REPO_ROOT / "src" / "jarvis"
    assert not [p for p in src.rglob("*.py") if "run_session_turn" in p.read_text()]


# -- 7. judging a failure in a short session (docs/superpowers/specs/2026-11-01-monitoring-orders.md §4)


def _judge(central, item, call, **kw) -> str:
    asked = kw.pop("asked", [])
    return morpheus.act(central, kw.pop("catalog", catalog()), item, cwd=PROD, call=call,
                        now=kw.pop("now", TEST_EPOCH),
                        investigate=kw.pop("investigate", lambda c, i: ("fo-inv00001", "")),
                        ask_neo=lambda c, i, note: asked.append((i, note)) or 9)


def test_a_rewrite_sets_the_judges_script_and_watches_again(central):
    item = _failed(central)
    central.add_morpheus_run(item["id"], exit=1, output="FAIL the job errored", now=TEST_EPOCH)
    call = FakeCall(reply={"verdict": "rewrite", "reason": "keys on the wrong field",
                           "script": "exit 0"})
    assert _judge(central, central.morpheus_item(item["id"]), call) == "rewrite"
    after = central.morpheus_item(item["id"])
    assert (after["state"], after["script"], after["script_source"], after["script_rewrites"],
            after["action"], after["attempts"]) == ("monitoring", "exit 0", "agent", 1, "", 0)
    assert after["deadline_ts"] == TEST_EPOCH + 3 * DAY and after["next_check_ts"] == TEST_EPOCH
    # Built from the row: its script, its runs, its counters
    prompt = call.calls[0]["prompt"]
    assert "exit 75" in prompt and "FAIL the job errored" in prompt
    assert "script exited 1" in prompt and "Script rewrites so far: 0 of 1" in prompt
    assert call.calls[0]["persist"] is True and call.calls[0]["cwd"] == PROD
    # No arming session follows: the row is not queued
    assert morpheus.owed_arming(central, now=TEST_EPOCH + 10 * DAY) == []
    [row] = central.conn.execute("SELECT label, wo_id FROM agent_calls").fetchall()
    assert tuple(row) == ("judge", item["id"])


def test_a_rewrite_at_the_cap_escalates_and_keeps_the_script(central):
    item = _failed(central)
    assert central.claim_morpheus_script_rewrite(item["id"], morpheus.MAX_SCRIPT_REWRITES)
    asked = []
    call = FakeCall(reply={"verdict": "rewrite", "reason": "wrong", "script": "exit 0"})
    assert _judge(central, central.morpheus_item(item["id"]), call, asked=asked) == "neo:9"
    after = central.morpheus_item(item["id"])
    assert (after["state"], after["script"], after["action"]) == ("failed", "exit 75", "neo:9")
    assert "rewrite(s) are spent" in asked[0][1]


def test_confirm_hands_off_with_the_judges_reading_marked_as_one(central):
    item = _failed(central)
    seen = []
    action = _judge(central, item, CONFIRM,
                    investigate=lambda c, i: seen.append(morpheus._why(i)) or ("fo-inv00001", ""))
    assert action == "investigation:fo-inv00001"
    assert central.morpheus_item(item["id"])["action"] == action
    assert "reading — NOT a diagnosis:\nthe job errors in the log" in seen[0]


def test_escalate_asks_neo_with_the_judges_reason(central):
    item = _failed(central)
    asked = []
    call = FakeCall(reply={"verdict": "escalate", "reason": "production is half deployed"})
    assert _judge(central, item, call, asked=asked) == "neo:9"
    assert asked[0][0]["judge_reason"] == "production is half deployed" and asked[0][1] == ""
    assert central.morpheus_item(item["id"])["action"] == "neo:9"


def test_a_confirm_with_no_pull_request_escalates(central):
    order = morpheus.create(central, project="proj_a", title="the nightly job", prompt="it runs")
    central.update_morpheus_item(order["id"], state="failed", resolution="script exited 1",
                                 script="exit 1", script_source="agent", last_exit=1)
    filed, asked = [], []
    assert _judge(central, central.morpheus_item(order["id"]), CONFIRM, asked=asked,
                  investigate=lambda c, i: filed.append(1) or ("x", "")) == "neo:9"
    assert filed == [] and "no pull request" in asked[0][1]
    assert "Monitoring order" in morpheus._why(asked[0][0])


def test_a_killed_judge_costs_no_attempt_until_max_kills_in_a_row(central):
    """A release restart kills once: same rule as arming (kn-1e43fe1a, kn-40db1828)"""
    item = _failed(central)
    killer, asked = FakeCall(error=sigkilled()), []
    for n in range(1, morpheus.MAX_KILLS):
        assert _judge(central, central.morpheus_item(item["id"]), killer, asked=asked) == ""
        after = central.morpheus_item(item["id"])
        assert (after["action"], after["attempts"], after["kills"], after["state"]) == \
            ("", 0, n, "failed")
    assert [i["id"] for i in morpheus.owed_actions(central)] == [item["id"]]
    _judge(central, central.morpheus_item(item["id"]), killer, asked=asked)
    after = central.morpheus_item(item["id"])
    assert (after["attempts"], after["kills"], after["action"]) == (1, 0, "")
    assert asked == []
    # An answer resets the run of kills
    central.update_morpheus_item(item["id"], kills=1)
    call = FakeCall(reply={"verdict": "escalate", "reason": "half deployed"})
    _judge(central, central.morpheus_item(item["id"]), call, asked=asked)
    assert central.morpheus_item(item["id"])["kills"] == 0


@pytest.mark.parametrize("call", [FakeCall(reply="no JSON here"),
                                  FakeCall(error=claude_cli.ClaudeCliError("timed out"))],
                         ids=["unparseable", "timeout"])
def test_a_judge_that_fails_releases_its_claim_then_escalates_without_a_verdict(central, call):
    item = _failed(central)
    asked = []
    for n in range(1, morpheus.MAX_OFFERS):
        assert _judge(central, central.morpheus_item(item["id"]), call, asked=asked) == ""
        after = central.morpheus_item(item["id"])
        assert (after["action"], after["attempts"], after["state"]) == ("", n, "failed")
    assert asked == []
    assert _judge(central, central.morpheus_item(item["id"]), call, asked=asked) == "neo:9"
    assert len(call.calls) == morpheus.MAX_OFFERS
    [(seen, note)] = asked
    assert "could not judge it" in note and "judge_reason" not in seen


def test_a_pre_claimed_order_gets_no_session(central):
    item = _failed(central)
    central.claim_morpheus_action(item["id"], "judge:1")
    call = FakeCall()
    assert morpheus.owed_actions(central) == []
    assert _judge(central, item, call) == "" and call.calls == []
    assert central.morpheus_item(item["id"])["action"] == "judge:1"


@pytest.mark.parametrize("state", ["cancelled", "validated"])
def test_a_settled_order_is_never_judged(central, state):
    item = _failed(central)
    central.update_morpheus_item(item["id"], state=state, action="")
    call = FakeCall()
    assert morpheus.owed_actions(central) == []
    assert _judge(central, central.morpheus_item(item["id"]), call) == "" and call.calls == []


def test_the_brake_and_the_usage_window_hold_judging_and_spend_nothing(central):
    item = _failed(central)
    morpheus.set_pause(central, "paused", "quiet", now=TEST_EPOCH)
    call = FakeCall()
    assert _judge(central, item, call) == "" and call.calls == []
    morpheus.set_pause(central, "running", "go", now=TEST_EPOCH)
    call = FakeCall(error=refused(TEST_EPOCH + 4000))
    assert _judge(central, item, call) == "usage_held"
    after = central.morpheus_item(item["id"])
    assert (after["action"], after["attempts"]) == ("", 0)
    assert _judge(central, after, call) == "" and len(call.calls) == 1


def test_a_hand_off_that_crashes_gives_its_claim_back(central):
    item = _failed(central)

    def boom(c, i):
        raise RuntimeError("steward down")

    with pytest.raises(RuntimeError):
        _judge(central, item, CONFIRM, investigate=boom)
    assert central.morpheus_item(item["id"])["action"] == ""


def test_a_stale_judging_claim_trips_the_invariant_and_is_cleared(central, monkeypatch):
    from jarvis import invariants

    item = _failed(central)
    central.claim_morpheus_action(item["id"], "judge:1")
    claimed = central.morpheus_item(item["id"])["action_ts"]
    stale = MorpheusConfig().turn_timeout_seconds * 3
    monkeypatch.setattr("jarvis.db.now", lambda: claimed + stale - 1)
    assert list(invariants.check_morpheus_moves(repair=True)) == []
    monkeypatch.setattr("jarvis.db.now", lambda: claimed + stale + 1)
    [v] = list(invariants.check_morpheus_moves(repair=True))
    assert v.invariant == "INV-MORPHEUS-OVERDUE" and v.repaired and "judging claim" in v.detail
    assert central.morpheus_item(item["id"])["action"] == ""
    assert invariants.check_morpheus_moves in invariants.OS_REPAIRABLE


def test_a_held_brake_is_not_an_overdue_action(central, monkeypatch):
    from jarvis import invariants

    item = _failed(central)
    morpheus.set_pause(central, "paused", "quiet", now=TEST_EPOCH)
    monkeypatch.setattr("jarvis.db.now",
                        lambda: float(central.morpheus_item(item["id"])["updated_ts"]) + 2 * 3600)
    assert list(invariants.check_morpheus_moves()) == []
    morpheus.set_pause(central, "running", "go", now=TEST_EPOCH)
    assert [v.invariant for v in invariants.check_morpheus_moves()] == ["INV-MORPHEUS-OVERDUE"]


@pytest.mark.parametrize("persist", [True, False])
def test_persist_alone_decides_whether_the_transcript_is_kept(monkeypatch, persist):
    seen = {}

    def fake_run(args, **kw):
        seen["args"] = list(args)
        return json.dumps({"result": "{}", "session_id": "s-1"})

    monkeypatch.setattr(claude_cli, "_run", fake_run)
    claude_cli.run_headless_result("hi", persist=persist)
    assert ("--no-session-persistence" in seen["args"]) is (not persist)
    assert "--output-format" in seen["args"]


# -- 6b. the surfaces say what Morpheus is DOING, from the rows (#1162, monitoring-orders §1.3)


def test_the_report_has_no_session_and_activity_has_the_contract_shape(central):
    queued(central, 2)
    rep = morpheus.report(central, catalog())
    assert set(rep) == {"enabled", "counts", "paused", "pause", "activity", "last_release",
                        "items"}
    assert rep["activity"] == {"state": "arming", "owed_arm": 1, "owed_judge": 0,
                               "usage_hold_until": None, "max_offers": morpheus.MAX_OFFERS}


def test_activity_reads_judging_then_idle_from_the_rows(central):
    item = _failed(central)
    assert morpheus.activity(central)["state"] == "judging"
    central.claim_morpheus_action(item["id"], "neo:1")
    assert morpheus.activity(central)["state"] == "idle"


def test_the_status_line_separates_the_pause_mode_from_the_activity(central, tmp_path, capsys,
                                                                    clock):
    from jarvis.cli import main

    queued(central, 2)
    assert main(["morpheus", "status"]) == 0
    out = capsys.readouterr().out
    assert "unpaused (follows the fleet) · arming · 1 owed arming" in out
    morpheus.arm(central, catalog(), tmp_path, now=db.now(),
                 call=FakeCall(error=refused(db.now() + 4000)))
    assert main(["morpheus", "status"]) == 0
    out = capsys.readouterr().out
    assert "usage_held" in out and "(usage limit)" in out and "held until" in out


def test_the_dashboard_renders_without_a_session(central):
    from fastapi.testclient import TestClient

    from jarvis.ui.app import create_app

    queued(central, 2)
    page = TestClient(create_app()).get("/mo")
    assert page.status_code == 200
    assert "1 owed arming" in page.text and "compaction" not in page.text


# -- 7. enforcing the section, in every project


def test_the_section_is_a_required_section_for_every_project():
    assert morpheus.SECTION in hooks.PR_BODY_SECTIONS
    bundled = REPO_ROOT / "src/jarvis/assets/skills/open-a-pull-request/pull_request_template.md"
    assert f"## {morpheus.SECTION}" in bundled.read_text()


def test_the_hook_denies_a_body_without_it(tmp_path):
    full = "\n\n".join(f"## {s}\nfilled" for s in hooks.PR_BODY_SECTIONS)
    without = "\n\n".join(f"## {s}\nfilled" for s in hooks.PR_BODY_SECTIONS
                          if s != morpheus.SECTION)
    assert hooks.pr_body_problems(full) == []
    assert hooks.pr_body_problems(without) == [f"no `## {morpheus.SECTION}` section"]


def test_ci_check_script():
    script = REPO_ROOT / "scripts/check_pr_validation.py"
    ok = subprocess.run([sys.executable, str(script)], input=HINTS_BODY, text=True,
                        capture_output=True, env={"PATH": "/usr/bin:/bin"})
    assert ok.returncode == 0 and "hints" in ok.stdout
    bad = subprocess.run([sys.executable, str(script)], text=True, capture_output=True,
                         env={"PATH": "/usr/bin:/bin", "PR_BODY": "## Summary\nx"})
    assert bad.returncode == 1 and "::error::" in bad.stdout


def test_the_workflow_reruns_on_an_edited_body():
    wf = (REPO_ROOT / ".github/workflows/pr-body.yml").read_text()
    assert "edited" in wf and "scripts/check_pr_validation.py" in wf


def test_the_skill_asks_for_hints_in_every_project():
    skill = (REPO_ROOT / "src/jarvis/assets/skills/open-a-pull-request/SKILL.md").read_text()
    assert morpheus.SECTION in skill and "every project" in skill and "hints" in skill


def test_shipit_queues_the_release_before_anything_restarts():
    text = (REPO_ROOT / "scripts/shipit.sh").read_text()
    queue = text.index("morpheus enqueue-release")
    assert queue < text.index("# --- 5b.") < text.index("# --- 6. restart the UI")


# -- 8. the catalog


def test_defaults_match_the_ask():
    cfg = parse_catalog({"projects": []}).os.morpheus
    assert (cfg.enabled, cfg.timeout_days, cfg.compact_at, cfg.on_failed, cfg.on_timeout) == \
        (True, 3.0, 0.30, "investigate", "neo")


@pytest.mark.parametrize("raw", [{"timeout_days": 0}, {"compact_at": 1.5},
                                 {"on_failed": "shrug"}, {"enabled": "yes"},
                                 {"check_every_minutes": 0}])
def test_bad_values_refuse_to_boot(raw):
    with pytest.raises(CatalogError):
        parse_catalog({"os": {"morpheus": raw}, "projects": []})


# -- 9. the invariant


def test_an_overdue_item_is_a_violation(central, monkeypatch):
    from jarvis import invariants

    monkeypatch.setattr(invariants, "_live_catalog", lambda: None)
    item = queued(central)
    monkeypatch.setattr("jarvis.db.now", lambda: TEST_EPOCH + 3 * DAY + 2 * 3600)
    [v] = list(invariants.check_morpheus_moves())
    assert v.invariant == "INV-MORPHEUS-OVERDUE" and v.subject_id == item["id"]
    assert invariants.check_morpheus_moves in invariants.OS_INVARIANTS


def test_an_owed_action_is_a_violation_and_a_taken_one_is_not(central, monkeypatch):
    from jarvis import invariants

    monkeypatch.setattr(invariants, "_live_catalog", lambda: None)
    item = queued(central)
    monkeypatch.setattr("jarvis.db.now", lambda: TEST_EPOCH)
    central.update_morpheus_item(item["id"], state="failed")
    monkeypatch.setattr("jarvis.db.now", lambda: TEST_EPOCH + 2 * 3600)
    assert [v.invariant for v in invariants.check_morpheus_moves()] == ["INV-MORPHEUS-OVERDUE"]
    central.update_morpheus_item(item["id"], action="neo:1")
    assert list(invariants.check_morpheus_moves()) == []


# -- 10. the daemon, the CLI and the dashboard


def test_the_daemon_pass_runs_each_check_in_its_project_and_hands_off(
        central, catalog_file, project, monkeypatch):
    from jarvis.catalog import load_catalog
    from jarvis.daemon import Daemon

    daemon = Daemon(load_catalog(catalog_file))
    good = monitoring(central, n=1, script="pwd > ran-here; exit 0")
    bad = monitoring(central, n=2, script="exit 1", title="no wo")
    central.update_morpheus_item(bad["id"], last_exit=1)  # its first exit 1 is behind it
    monkeypatch.setattr(morpheus, "_ask_neo", lambda c, i, note="": 42)
    judged = []

    def judge(central, catalog, item, *, purpose, cwd, call=None):
        judged.append((item["id"], purpose, cwd))
        return {"verdict": "escalate", "reason": "cannot tell"}

    monkeypatch.setattr(morpheus, "run_session", judge)
    daemon._morpheus_pass()
    assert central.morpheus_item(good["id"])["state"] == "validated"
    assert (project / "ran-here").read_text().strip() == str(project)
    after = central.morpheus_item(bad["id"])
    assert after["state"] == "failed" and after["action"] == "neo:42"
    assert judged == [(bad["id"], "judge", project)]


def test_cli_status_and_script(central, tmp_path, capsys):
    from jarvis.cli import main

    item = queued(central, 2)
    (tmp_path / "s.sh").write_text("exit 75\n")
    assert main(["morpheus", "script", item["id"], "--file", str(tmp_path / "s.sh")]) == 0
    capsys.readouterr()
    assert main(["morpheus", "status", "--json"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["counts"]["monitoring"] == 1 and rep["items"][0]["script"] == "exit 75"
    assert main(["morpheus", "show", "mv-nope"]) == 1


def test_cli_show_lists_the_recorded_runs(central, capsys):
    from jarvis.cli import main

    item = queued(central)
    central.update_morpheus_item(item["id"], checks=1)
    assert main(["morpheus", "show", item["id"]]) == 0
    assert "history starts with this release" in capsys.readouterr().out
    central.add_morpheus_run(item["id"], exit=1, output="boom", kind="probe",
                             state_after="failed")
    assert main(["morpheus", "show", item["id"]]) == 0
    out = capsys.readouterr().out.split("runs:")[1]
    assert "probe" in out and "exit 1" in out and "failed" in out


def test_the_dashboard_shows_a_link_not_the_pr_s_text(central):
    from fastapi.testclient import TestClient

    from jarvis.ui.app import create_app

    item = _failed(central)
    page = TestClient(create_app()).get("/mo")
    assert page.status_code == 200
    assert '<a href="https://github.com/o/r/pull/7" title="[wo-1234abcd] thing">#7</a>' \
        in page.text
    assert "proj_a" in page.text and "failed" in page.text
    assert "Watch the dashboard" not in page.text
    # The one-line why sits in the needs-you strip, not the PR's text
    assert page.text.split("needs-you\">")[1].index(item["resolution"]) < page.text.split("needs-you\">")[1].index("mv-list")


def test_morpheus_unpause_runs_its_session_through_a_fleet_pause(central):
    from jarvis import fleet

    fleet.pause(central, "token burn", now=TEST_EPOCH)
    assert morpheus.pause_state(central)["paused"] is True
    state = morpheus.set_pause(central, "running", "validate 0.12.5", now=TEST_EPOCH)
    assert state["paused"] is False and state["fleet_paused"] is True
    assert morpheus.report(central)["paused"] is False
    assert morpheus.set_pause(central, None)["paused"] is True


def test_morpheus_pause_holds_it_while_the_fleet_runs(central):
    state = morpheus.set_pause(central, "paused", "noisy checks", now=TEST_EPOCH)
    assert state == {"paused": True, "override": "paused", "reason": "noisy checks",
                     "since": TEST_EPOCH, "fleet_paused": False}
    assert morpheus.set_pause(central, None)["paused"] is False
    with pytest.raises(ValueError):
        morpheus.set_pause(central, "maybe")


def test_an_unreadable_morpheus_pause_is_still_a_brake(central):
    central.set_state(morpheus.PAUSE_KEY, "{not json")
    assert morpheus.pause_state(central)["paused"] is True


def test_the_cli_pins_and_releases_the_morpheus_pause(central, capsys):
    from jarvis import fleet
    from jarvis.cli import main

    fleet.pause(central, "token burn", now=TEST_EPOCH)
    assert main(["morpheus", "unpause", "--reason", "validate the release"]) == 0
    assert "RUNNING by `morpheus unpause` (fleet paused)" in capsys.readouterr().out
    assert main(["morpheus", "status"]) == 0
    assert "RUNNING by `morpheus unpause`" in capsys.readouterr().out
    assert main(["morpheus", "follow-fleet"]) == 0
    assert "PAUSED with the fleet" in capsys.readouterr().out


def test_the_dashboard_links_each_item_s_id_to_its_detail(central):
    from fastapi.testclient import TestClient

    from jarvis.ui.app import create_app

    item = _failed(central)
    morpheus.set_pause(central, "running", "validate the release", now=TEST_EPOCH)
    page = TestClient(create_app()).get("/mo")
    assert f'<a class="rowid" href="/mo/{item["id"]}">{item["id"]}</a>' in page.text
    assert "jarvis morpheus unpause" in page.text and "validate the release" in page.text


def test_the_dashboard_shows_the_activity_and_its_backoff_not_only_the_pause_mode(central,
                                                                                  tmp_path):
    """#1171: the pause mode read healthy while the usage window held every turn"""
    from fastapi.testclient import TestClient

    from jarvis.ui.app import create_app

    item = queued(central, 2)
    morpheus.arm(central, catalog(), tmp_path, now=db.now(),
                 call=FakeCall(error=refused(db.now() + 4000)))
    page = TestClient(create_app()).get("/mo").text
    assert "unpaused (follows the fleet)" in page and "(usage limit)" in page
    # The refusal cost the order nothing
    assert central.morpheus_item(item["id"])["attempts"] == 0


def test_check_and_output_live_on_the_detail_not_in_the_list(central):
    from fastapi.testclient import TestClient

    from jarvis.ui.app import create_app

    item = _failed(central)
    central.update_morpheus_item(item["id"], last_output="boom <x>")
    client = TestClient(create_app())
    assert "boom" not in client.get("/mo").text
    page = client.get(f"/mo/{item['id']}").text
    assert "boom &lt;x&gt;" in page and 'id="tab-output"' in page
    assert item["script"] in page and 'id="tab-script"' in page


def test_the_list_filters_by_state_and_puts_failures_first(central):
    from fastapi.testclient import TestClient

    from jarvis.ui.app import create_app

    failed = _failed(central)
    later = queued(central, 8)
    central.update_morpheus_item(later["id"], hints="newer than the failure")
    client = TestClient(create_app())
    page = client.get("/mo").text
    table = page.split('class="mv-list"')[1]
    assert table.index(failed["id"]) < table.index(later["id"])
    narrowed = client.get("/mo?state=queued").text.split('class="mv-list"')[1]
    assert later["id"] in narrowed and failed["id"] not in narrowed
    assert failed["id"] not in client.get("/mo?state=bogus").text.split('class="mv-list"')[1]


def test_a_confirmed_failure_leaves_the_needs_you_strip(central):
    from fastapi.testclient import TestClient

    from jarvis.ui.app import create_app

    item = _failed(central)
    central.update_morpheus_item(item["id"], resolution="confirmed failed: fix filed")
    assert "needs-you\">" not in TestClient(create_app()).get("/mo").text


def test_the_detail_shows_runs_questions_and_handoff(central):
    from fastapi.testclient import TestClient

    from jarvis.ui.app import create_app

    item, qid = _escalated(central)
    central.add_morpheus_run(item["id"], exit=1, output="tail <y>", kind="check",
                             state_after="failed")
    page = TestClient(create_app()).get(f"/mo/{item['id']}").text
    assert "tail &lt;y&gt;" in page
    assert f'href="/neo/question/{qid}"' in page and "escalated" in page
    assert f'Neo <a href="/neo/question/{qid}">q{qid}</a>' in page
    assert f'action="/mo/{item["id"]}/confirm"' in page
    assert f'name="next" value="/mo/{item["id"]}"' in page


def test_the_detail_says_history_starts_when_checks_predate_runs(central):
    from fastapi.testclient import TestClient

    from jarvis.ui.app import create_app

    item = _failed(central)
    central.update_morpheus_item(item["id"], checks=3)
    page = TestClient(create_app()).get(f"/mo/{item['id']}").text
    assert "history starts with this release" in page


def test_an_unknown_item_detail_is_a_404(central):
    from fastapi.testclient import TestClient

    from jarvis.ui.app import create_app

    r = TestClient(create_app()).get("/mo/mv-deadbeef")
    assert r.status_code == 404 and "mv-deadbeef" in r.text


def test_a_detail_action_lands_back_on_the_detail(central):
    from fastapi.testclient import TestClient

    from jarvis.ui.app import create_app

    item = _failed(central)
    r = TestClient(create_app()).post(f"/mo/{item['id']}/requeue", follow_redirects=False,
                                     data={"next": f"/mo/{item['id']}"})
    assert r.headers["location"] == f"/mo/{item['id']}"


def test_a_failed_item_offers_all_three_decisions_and_links_its_neo_question(central):
    """Issue #1302: list row links Neo question; its detail offers confirm, waive, requeue"""
    from fastapi.testclient import TestClient

    from jarvis.ui.app import create_app

    item = _failed(central)
    central.claim_morpheus_action(item["id"], "neo:2259")
    client = TestClient(create_app())
    assert 'Neo <a href="/neo/question/2259">q2259</a>' in client.get("/mo").text
    page = client.get(f"/mo/{item['id']}").text
    for verb in ("confirm", "waive", "requeue"):
        assert f'action="/mo/{item["id"]}/{verb}"' in page


# -- monitoring orders: the record (docs/superpowers/specs/2026-11-01-monitoring-orders.md §2)

# `morpheus_items` exactly as releases before monitoring orders shipped it
OLD_MORPHEUS_ITEMS_DDL = """
CREATE TABLE morpheus_items (
    id TEXT PRIMARY KEY,
    project TEXT NOT NULL DEFAULT '',
    repo TEXT NOT NULL DEFAULT '',
    pr_number INTEGER NOT NULL,
    pr_url TEXT NOT NULL DEFAULT '',
    pr_title TEXT NOT NULL DEFAULT '',
    wo_id TEXT NOT NULL DEFAULT '',
    release_tag TEXT NOT NULL DEFAULT '',
    hints TEXT NOT NULL DEFAULT '',
    script TEXT NOT NULL DEFAULT '',
    script_source TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL,
    created_ts REAL NOT NULL,
    updated_ts REAL NOT NULL,
    deadline_ts REAL NOT NULL,
    last_check_ts REAL,
    next_check_ts REAL,
    checks INTEGER NOT NULL DEFAULT 0,
    last_exit INTEGER,
    last_output TEXT NOT NULL DEFAULT '',
    resolution TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL DEFAULT '',
    action_ts REAL,
    auto_requeues INTEGER NOT NULL DEFAULT 0,
    last_check_version TEXT NOT NULL DEFAULT '',
    script_rewrites INTEGER NOT NULL DEFAULT 0,
    rewrite_brief TEXT NOT NULL DEFAULT '',
    UNIQUE (repo, pr_number)
);
CREATE INDEX idx_morpheus_items_state ON morpheus_items(state);
CREATE TABLE IF NOT EXISTS os_state (key TEXT PRIMARY KEY, value TEXT);
"""


def _old_home() -> Path:
    import sqlite3

    from jarvis.paths import central_db_path

    path = central_db_path()  # a fixture may have opened it with today's SCHEMA: undo that
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute("DROP TABLE IF EXISTS monitoring_orders")
    conn.executescript(OLD_MORPHEUS_ITEMS_DDL)
    rows = [("mv-aaaa1111", "jarvis_os", 7, "jarvis-0.1.1", "queued", "", 0),
            ("mv-bbbb2222", "proj_a", 8, "", "failed", "neo:12", 1)]
    for item_id, project, n, tag, state, action, exit_code in rows:
        conn.execute("INSERT INTO morpheus_items (id, project, repo, pr_number, release_tag, "
                     "state, action, last_exit, created_ts, updated_ts, deadline_ts) "
                     "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                     (item_id, project, "o/r", n, tag, state, action, exit_code,
                      TEST_EPOCH, TEST_EPOCH, TEST_EPOCH + DAY))
    session = {"turns": 3, "offers": {"mv-aaaa1111": {"n": 2, "ts": TEST_EPOCH + 5,
                                                      "cause": "offer"}}}
    conn.execute("INSERT OR REPLACE INTO os_state (key, value) VALUES (?, ?)",
                 ("morpheus.session", json.dumps(session)))
    conn.commit()
    conn.close()
    return path


def _tables(central) -> set[str]:
    return {r["name"] for r in central.conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'")}


def test_an_old_home_moves_into_monitoring_orders_once(cfg):
    _old_home()
    central = CentralStore()
    try:
        assert "morpheus_items" not in _tables(central)
        rows = {i["id"]: i for i in central.morpheus_items()}
        assert set(rows) == {"mv-aaaa1111", "mv-bbbb2222"}
        a, b = rows["mv-aaaa1111"], rows["mv-bbbb2222"]
        assert (a["state"], a["attempts"], a["last_attempt_ts"], a["origin"]) == \
            ("queued", 2, TEST_EPOCH + 5, "release")
        assert (b["state"], b["action"], b["attempts"], b["origin"]) == \
            ("failed", "neo:12", 0, "merge")
        before = central.morpheus_items()
    finally:
        central.close()
    again = CentralStore()
    try:
        assert again.morpheus_items() == before and "morpheus_items" not in _tables(again)
        # The PR uniqueness survived the rebuild: the same PR is never queued twice
        assert queued(again, 7) is None
    finally:
        again.close()


def test_a_fresh_home_never_creates_morpheus_items(central):
    assert "monitoring_orders" in _tables(central) and "morpheus_items" not in _tables(central)


def test_orders_with_no_pr_coexist_and_mint_mo_ids(central):
    a = morpheus.create(central, project="jarvis_os", title="disk", prompt="watch the disk")
    b = morpheus.create(central, project="jarvis_os", title="cpu", script="exit 75",
                        every_minutes=5)
    assert a["id"] != b["id"] and all(i["id"].startswith("mo-") for i in (a, b))
    assert a["pr_number"] is None and b["pr_number"] is None
    assert (a["state"], a["script"], a["origin"], a["prompt"]) == \
        ("queued", "", "user", "watch the disk")
    assert (b["state"], b["script"], b["script_source"], b["every_minutes"]) == \
        ("queued", "exit 75", "user", 5)
    for bad in ({}, {"prompt": "x", "script": "exit 0"}):
        with pytest.raises(ValueError):
            morpheus.create(central, project="jarvis_os", title="t", **bad)
    # A release-queued PR is a mo- order too
    assert queued(central, 9)["id"].startswith("mo-")


def test_a_user_script_is_queued_until_one_arm_session_validates_it(central, tmp_path,
                                                                    monkeypatch):
    """Spec §1: creating a script order opens a short session that validates it"""
    item = morpheus.create(central, project="jarvis_os", title="cpu", script="exit 75")
    assert morpheus.owed_arming(central, now=TEST_EPOCH)[0]["id"] == item["id"]
    seen = []

    def fake_run(args, **kw):
        seen.append(list(args))
        reply = json.dumps({"script": "exit 75\n", "notes": "keeps the contract"})
        return json.dumps({"result": reply, "session_id": "s-1"})

    monkeypatch.setattr(claude_cli, "_run", fake_run)
    assert morpheus.arm(central, catalog(), tmp_path, now=TEST_EPOCH)["armed"] == 1
    assert len(seen) == 1 and "The user wrote this script" in " ".join(seen[0])
    after = central.morpheus_item(item["id"])
    assert (after["state"], after["script"], after["script_source"]) == \
        ("monitoring", "exit 75", "user")


def test_mo_create_defaults_the_deadline_from_the_live_catalog(central, monkeypatch):
    from jarvis import ops

    assert MorpheusConfig().timeout_days != 5
    monkeypatch.setattr(ops, "_morpheus_catalog", lambda: catalog(timeout_days=5))
    monkeypatch.setattr("jarvis.db.now", lambda: TEST_EPOCH)
    item = ops.mo_create("jarvis_os", "disk", prompt="watch the disk")
    assert item["deadline_ts"] == TEST_EPOCH + 5 * DAY
    # An explicit deadline still wins
    item = ops.mo_create("jarvis_os", "cpu", prompt="watch the cpu", deadline="2d")
    assert item["deadline_ts"] == TEST_EPOCH + 2 * DAY


def test_check_honours_the_orders_own_interval(central, cfg, tmp_path):
    assert cfg.check_every_minutes != 7
    own = morpheus.create(central, project="jarvis_os", title="t", script="exit 75",
                          every_minutes=7)
    morpheus.set_script(central, own["id"], "exit 75", "user")  # armed
    default = monitoring(central)
    for item in (own, default):
        morpheus.check(central, cfg, central.morpheus_item(item["id"]), tmp_path,
                       now=TEST_EPOCH, run=_run(75), version=lambda where: "")
    assert central.morpheus_item(own["id"])["next_check_ts"] == TEST_EPOCH + 7 * 60
    assert central.morpheus_item(default["id"])["next_check_ts"] == \
        TEST_EPOCH + cfg.check_every_minutes * 60


def test_cancel_stops_an_open_order_and_sweep_leaves_it(central, cfg, tmp_path):
    item = monitoring(central)
    control = queued(central, 8)
    for i in (item, control):
        central.update_morpheus_item(i["id"], deadline_ts=TEST_EPOCH - 1)
    done = morpheus.cancel(central, item["id"])
    assert (done["state"], done["next_check_ts"]) == ("cancelled", None)
    morpheus.sweep(central, cfg, tmp_path, now=TEST_EPOCH, run=_run(0))
    # The control proves sweep would have moved an overdue open order
    assert central.morpheus_item(control["id"])["state"] == "timedout"
    assert central.morpheus_item(item["id"])["state"] == "cancelled"
    with pytest.raises(ValueError):
        morpheus.cancel(central, item["id"])
    # A cancelled PR is never queued again: a release adds only PRs it has not seen
    assert queued(central) is None
    assert "cancelled" in morpheus.TERMINAL_STATES and "cancelled" not in morpheus.ACTION_STATES


@pytest.mark.parametrize("value,expected", [("mv-1234abcd", True), ("mo-1234abcd", True),
                                            ("fo-1234abcd", False), ("wo-1234abcd", False),
                                            (None, False)])
def test_one_predicate_names_a_monitoring_order(value, expected):
    assert morpheus.is_monitoring_order_id(value) is expected


def test_go_opens_both_id_shapes_on_the_morpheus_page():
    from jarvis import ops

    for rid in ("mv-1234abcd", "mo-1234abcd"):
        assert ops.resolve_ref(rid) == f"/mo/{rid}"


# -- monitoring orders: the dashboard (docs/superpowers/specs/2026-11-01-monitoring-orders.md §6)


def _client():
    from fastapi.testclient import TestClient

    from jarvis.ui.app import create_app

    return TestClient(create_app())


def test_the_old_morpheus_paths_redirect_permanently(central):
    item = _failed(central)
    client = _client()
    r = client.get("/morpheus?state=failed", follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (301, "/mo?state=failed")
    r = client.get(f"/morpheus/{item['id']}", follow_redirects=False)
    assert (r.status_code, r.headers["location"]) == (301, f"/mo/{item['id']}")
    # Both old paths still land on a rendered page
    assert item["id"] in client.get("/morpheus").text
    assert client.get(f"/morpheus/{item['id']}").status_code == 200


def test_the_list_shows_origin_title_and_frequency_and_creates_a_monitor(central):
    client = _client()
    r = client.post("/mo/new", follow_redirects=False,
                    data={"project": "proj_a", "title": "nightly backup ran",
                          "prompt": "the backup log has today's line", "every_minutes": "30"})
    assert r.status_code == 303
    [item] = central.morpheus_items()
    assert r.headers["location"] == f"/mo/{item['id']}"
    assert item["id"].startswith("mo-") and item["every_minutes"] == 30
    row = client.get("/mo").text.split(f'<tr id="{item["id"]}">')[1].split("</tr>")[0]
    assert "nightly backup ran" in row and ">user<" in row and "30 min" in row
    assert f'action="/mo/{item["id"]}/cancel"' in row
    detail = client.get(r.headers["location"]).text
    assert "the backup log has today&#39;s line" in detail


def test_a_monitor_without_a_prompt_is_refused_and_creates_nothing(central):
    r = _client().post("/mo/new", follow_redirects=False,
                       data={"project": "proj_a", "title": "t", "prompt": " "})
    assert r.status_code == 303 and "error=" in r.headers["location"]
    assert central.morpheus_items() == []


def test_cancel_stops_an_open_order_and_is_offered_only_on_open_ones(central):
    open_item, settled = queued(central, 3), _failed(central)
    client = _client()
    page = client.get("/mo").text
    assert f'action="/mo/{open_item["id"]}/cancel"' in page
    assert f'action="/mo/{settled["id"]}/cancel"' not in page
    r = client.post(f"/mo/{open_item['id']}/cancel", follow_redirects=False,
                    data={"next": f"/mo/{open_item['id']}"})
    assert r.headers["location"] == f"/mo/{open_item['id']}"
    assert central.morpheus_item(open_item["id"])["state"] == "cancelled"
    r = client.post(f"/mo/{settled['id']}/cancel", follow_redirects=False, data={})
    assert "error=" in r.headers["location"]
    assert central.morpheus_item(settled["id"])["state"] == "failed"


def test_the_detail_shows_its_session_and_cost_only_when_the_bill_exists(central, monkeypatch):
    from jarvis import bill

    item = _failed(central)
    central.update_morpheus_item(item["id"], last_session_id="sess-42")
    client = _client()
    monkeypatch.delattr(bill, "for_monitoring_order")
    page = client.get(f"/mo/{item['id']}").text
    assert "sess-42" in page and f"jarvis cost {item['id']}" not in page
    monkeypatch.setattr(bill, "for_monitoring_order",
                        lambda mo_id: {"total": {"calls": 2, "cost": {"list_usd": 1.5}}},
                        raising=False)
    page = client.get(f"/mo/{item['id']}").text
    assert "$1.50" in page and f"jarvis cost {item['id']}" in page
