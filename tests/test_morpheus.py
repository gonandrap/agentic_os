"""Morpheus validates shipped PRs in production (docs/superpowers/specs/2026-10-21-morpheus-validates-releases-in-production.md)"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from jarvis import claude_cli, hooks, morpheus
from jarvis.catalog import Catalog, CatalogError, MorpheusConfig, OsConfig, parse_catalog
from jarvis.central_store import CentralStore
from jarvis.testing import TEST_EPOCH

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


# -- 4. the mechanical half: the exit code and nothing else


def _run(exit_code, seen=None):
    def run(script, cwd, timeout):
        if seen is not None:
            seen.append(cwd)
        return morpheus.CheckResult(exit_code, f"said {exit_code}")
    return run


@pytest.mark.parametrize("code,state,resolution", [
    (0, "validated", "script exited 0"), (1, "failed", "script exited 1"),
    (75, "monitoring", ""), (None, "monitoring", "")])
def test_the_exit_code_is_the_verdict(central, cfg, tmp_path, code, state, resolution):
    item = monitoring(central)
    moved = morpheus.sweep(central, cfg, tmp_path, now=TEST_EPOCH, run=_run(code))
    after = central.morpheus_item(item["id"])
    assert (after["state"], after["resolution"]) == (state, resolution) and moved[state] == 1
    assert after["checks"] == 1 and after["last_output"] == f"said {code}"
    if state == "monitoring":
        assert after["next_check_ts"] == TEST_EPOCH + cfg.check_every_minutes * 60


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


def _failed(central) -> dict:
    item = monitoring(central)
    central.update_morpheus_item(item["id"], state="failed", resolution="script exited 1",
                                 last_exit=1)
    return central.morpheus_item(item["id"])


def test_failed_with_a_work_order_files_an_investigation_once(central):
    item = _failed(central)
    asked = []
    action = morpheus.act(central, catalog(), item,
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
    action = morpheus.act(central, catalog(), _failed(central),
                          investigate=lambda c, i: ("", "the steward said no"),
                          ask_neo=lambda c, i, note: notes.append(note) or 9)
    assert action == "neo:9" and notes == ["the steward said no"]


def test_timedout_asks_neo_by_default_and_the_route_is_configurable(central):
    item = queued(central)
    central.update_morpheus_item(item["id"], state="timedout")
    filed = []
    assert morpheus.act(central, catalog(), central.morpheus_item(item["id"]),
                        investigate=lambda c, i: filed.append(1) or ("x", ""),
                        ask_neo=lambda *a: 3) == "neo:3"
    assert filed == []
    central.update_morpheus_item(item["id"], action="")
    assert morpheus.act(central, catalog(on_timeout="investigate"),
                        central.morpheus_item(item["id"]),
                        investigate=lambda c, i: ("fo-x", ""),
                        ask_neo=lambda *a: 3) == "investigation:fo-x"


def test_no_work_order_means_no_investigation(central):
    item = queued(central, title="untitled")
    assert morpheus._investigate(catalog(), item) == \
        ("", "the PR names no work order to investigate")


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
    assert f"jarvis morpheus requeue {item['id']}" in q["question"]


@pytest.mark.parametrize("escalate,level", [(True, "warning"), (False, "info")])
def test_neo_s_verdict_is_recorded_and_reaches_the_inbox(central, escalate, level):
    item = queued(central)
    central.update_morpheus_item(item["id"], state="timedout", resolution="no verdict")
    q = {"id": 5, "project": "proj_a", "context": json.dumps({"morpheus_item": item["id"]})}
    morpheus.record_neo_verdict(central, q, {"escalate": escalate, "answer": "rare path",
                                             "reason": ""}, "http://x/neo/question/5")
    after = central.morpheus_item(item["id"])
    assert after["state"] == "timedout" and after["resolution"].endswith("Neo: rare path")
    rows = central.conn.execute("SELECT level, title FROM inbox").fetchall()
    assert [(r["level"], "PR #7" in r["title"]) for r in rows] == [(level, True)]


def test_a_validation_question_cannot_be_answered_like_a_worker_question(central):
    from jarvis import ops

    item = queued(central)
    qid = morpheus._ask_neo(catalog(), item, "")
    with pytest.raises(ops.OpsError, match=f"jarvis morpheus waive {item['id']}"):
        ops.neo_answer_escalated(qid, "looks fine")


# -- 6. the only LLM: Morpheus's session turns hints into scripts


class FakeTurn:
    def __init__(self, share: float = 0.1, fail: bool = False):
        self.calls: list[dict] = []
        self.share, self.fail = share, fail

    def __call__(self, prompt, session_id, **kw):
        self.calls.append({"prompt": prompt, "session_id": session_id, **kw})
        if self.fail:
            raise claude_cli.ClaudeCliError("boom")
        usage = {"context_peak": int(self.share * 200_000), "context_window": 200_000,
                 "input_tokens": 10, "output_tokens": 5}
        return claude_cli.HeadlessResult(text="mv-x: checks it", usage=usage,
                                         session_id=session_id, model="sonnet")


def test_no_queued_pr_means_no_turn(central, tmp_path):
    turn = FakeTurn()
    monitoring(central)
    assert morpheus.wake(central, catalog(), tmp_path, now=TEST_EPOCH, call=turn) == "idle"
    assert turn.calls == []


def test_a_queued_pr_wakes_one_turn_that_spawns_subagents(central, tmp_path):
    turn = FakeTurn()
    item = queued(central, 2, body("Watch the inbox."))
    dirs = {"proj_a": tmp_path / "proj_a"}
    assert morpheus.wake(central, catalog(), tmp_path, dirs=dirs, now=TEST_EPOCH,
                         call=turn) == "scripts"
    [call] = turn.calls
    assert call["resume"] is False and item["id"] in call["prompt"]
    assert "Watch the inbox." in call["prompt"]
    assert f"production directory: {tmp_path / 'proj_a'}" in call["prompt"]
    assert call["add_dirs"] == [tmp_path / "proj_a"]
    assert "spawn ONE subagent" in call["append_system_prompt"]
    assert "never diagnose a failure" in call["append_system_prompt"]
    assert "jarvis morpheus script" in call["append_system_prompt"]
    assert call["model"] == "sonnet"
    rows = central.conn.execute("SELECT kind, label FROM agent_calls").fetchall()
    assert [(r["kind"], r["label"]) for r in rows] == [("morpheus", "scripts")]
    # Offered once: not again until REOFFER_SECONDS, never past MAX_OFFERS
    assert morpheus.wake(central, catalog(), tmp_path, now=TEST_EPOCH + 60, call=turn) == "idle"
    for n in range(1, morpheus.MAX_OFFERS):
        morpheus.wake(central, catalog(), tmp_path,
                      now=TEST_EPOCH + n * morpheus.REOFFER_SECONDS + 1, call=turn)
    assert len(turn.calls) == morpheus.MAX_OFFERS
    assert morpheus.wake(central, catalog(), tmp_path, now=TEST_EPOCH + 10 * DAY,
                         call=turn) == "idle"
    assert all(c["session_id"] == call["session_id"] for c in turn.calls)
    assert all(c["resume"] for c in turn.calls[1:])


def test_past_the_compact_share_the_next_turn_is_a_compaction(central, tmp_path):
    turn = FakeTurn(share=0.31)
    queued(central, 2)
    morpheus.wake(central, catalog(), tmp_path, now=TEST_EPOCH, call=turn)
    assert morpheus.session_state(central)["compact_due"] is True
    assert morpheus.wake(central, catalog(), tmp_path, now=TEST_EPOCH + 60,
                         call=turn) == "compact"
    assert turn.calls[-1]["prompt"] == claude_cli.COMPACT_PROMPT and turn.calls[-1]["resume"]
    state = morpheus.session_state(central)
    assert state["compactions"] == 1 and state["compact_due"] is False


def test_under_the_share_nothing_compacts(central, tmp_path):
    turn = FakeTurn(share=0.29)
    queued(central, 2)
    morpheus.wake(central, catalog(), tmp_path, now=TEST_EPOCH, call=turn)
    assert morpheus.session_state(central)["compact_due"] is False


def test_a_pause_stops_the_session_and_nothing_else(central, tmp_path):
    turn = FakeTurn()
    queued(central, 2)
    assert morpheus.wake(central, catalog(), tmp_path, paused=True, now=TEST_EPOCH,
                         call=turn) == "paused"
    assert turn.calls == []


def test_a_failed_turn_is_recorded_and_still_counts_as_an_offer(central, tmp_path):
    turn = FakeTurn(fail=True)
    queued(central, 2)
    assert morpheus.wake(central, catalog(), tmp_path, now=TEST_EPOCH, call=turn) == "error"
    state = morpheus.session_state(central)
    assert state["last_error"] == "boom" and state["errors"] == 1
    # The CLI may have half-created the session: the next first turn mints a fresh id
    assert "session_id" not in state
    assert morpheus.wake(central, catalog(), tmp_path, now=TEST_EPOCH + 60, call=turn) == "idle"


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
    monkeypatch.setattr(morpheus, "_ask_neo", lambda c, i, note="": 42)
    daemon._morpheus_pass()
    assert central.morpheus_item(good["id"])["state"] == "validated"
    assert (project / "ran-here").read_text().strip() == str(project)
    after = central.morpheus_item(bad["id"])
    assert after["state"] == "failed" and after["action"] == "neo:42"


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


def test_the_dashboard_shows_a_link_not_the_pr_s_text(central):
    from fastapi.testclient import TestClient

    from jarvis.ui.app import create_app

    item = _failed(central)
    page = TestClient(create_app()).get("/morpheus")
    assert page.status_code == 200
    assert '<a href="https://github.com/o/r/pull/7" title="[wo-1234abcd] thing">#7</a>' \
        in page.text
    assert "proj_a" in page.text and "failed" in page.text
    assert item["resolution"] not in page.text and "Watch the dashboard" not in page.text


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


def test_the_dashboard_shows_each_item_s_id_to_copy(central):
    from fastapi.testclient import TestClient

    from jarvis.ui.app import create_app

    item = _failed(central)
    morpheus.set_pause(central, "running", "validate the release", now=TEST_EPOCH)
    page = TestClient(create_app()).get("/morpheus")
    assert f'title="click to select, then copy">{item["id"]}</td>' in page.text
    assert "jarvis morpheus unpause" in page.text and "validate the release" in page.text


def test_the_dashboard_opens_check_and_output_in_a_modal_not_in_the_table(central):
    from fastapi.testclient import TestClient

    from jarvis.ui.app import create_app

    item = _failed(central)
    central.update_morpheus_item(item["id"], last_output="boom <x>")
    page = TestClient(create_app()).get("/morpheus").text
    assert "<details>" not in page
    for kind in ("check", "tail"):
        assert f'data-peek="{item["id"]}-{kind}"' in page
        assert f'<dialog id="{item["id"]}-{kind}"' in page
    assert "boom &lt;x&gt;" in page
