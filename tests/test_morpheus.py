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

SCRIPT_BODY = """## Summary
x

## How to validate in production

Watch the dashboard.

```bash
# exit 0 = validated
jarvis status --json | grep -q morpheus
```

## Screenshots
None.
"""


def body(section: str) -> str:
    return f"## Summary\nx\n\n## How to validate in production\n\n{section}\n\n## Screenshots\n-\n"


def pr(n: int = 7, text: str = SCRIPT_BODY, title: str = "[wo-1234abcd] thing") -> morpheus.PullRequestBody:
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


# -- 1. the PR section


def test_a_bash_block_is_the_script_and_comments_inside_it_are_not_headings():
    raw = morpheus.find_section(SCRIPT_BODY)
    assert raw is not None and "Screenshots" not in raw
    section = morpheus.parse_section(raw)
    assert section.script.startswith("# exit 0 = validated")
    assert "grep -q morpheus" in section.script
    assert not section.waived and not section.empty


def test_prose_alone_is_hints_without_a_script():
    section = morpheus.parse_section(morpheus.find_section(body("Watch the inbox.")) or "")
    assert section.text == "Watch the inbox." and section.script == "" and not section.waived


def test_a_non_shell_fence_is_not_run():
    section = morpheus.parse_section("```json\n{\"a\": 1}\n```")
    assert section.script == ""


@pytest.mark.parametrize("text,reason", [("None — docs only", "docs only"),
                                         ("None.", "no reason given"),
                                         ("- None: tests only", "tests only")])
def test_none_waives_validation(text, reason):
    assert morpheus.parse_section(text).waived == reason


def test_prose_starting_with_none_is_not_a_waiver():
    assert morpheus.parse_section("None of the old paths change; watch the inbox.").waived == ""


def test_the_shipped_template_is_empty_and_its_comment_fence_does_not_swallow_the_body():
    template = (REPO_ROOT / ".github/pull_request_template.md").read_text()
    raw = morpheus.find_section(template)
    assert raw is not None and "Screenshots" not in raw
    assert morpheus.parse_section(raw).empty
    assert morpheus.section_problem(template) == \
        f"`## {morpheus.SECTION}` is still the empty template"


def test_a_body_without_the_section_names_the_fix():
    assert "no `## How to validate in production` section" in \
        morpheus.section_problem("## Summary\nx\n")
    assert morpheus.section_problem(SCRIPT_BODY) is None


# -- 2. which PRs a release carries


def test_pr_numbers_from_merge_and_squash_subjects():
    subjects = ["Merge pull request #12 from o/branch", "Fix the thing (#15)",
                "Release jarvis-0.1.1", "Merge pull request #12 from o/again", "no pr here"]
    assert morpheus.pr_numbers(subjects) == [12, 15]


def test_the_work_order_comes_from_the_title_prefix():
    assert morpheus.wo_of_title("[wo-1234abcd] thing") == "wo-1234abcd"
    assert morpheus.wo_of_title("thing [wo-1234abcd]") == ""


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
        return pr(n, body("Watch it.") if n == 2 else SCRIPT_BODY, title=f"PR {n}")

    out = morpheus.enqueue_release(central, cfg, release_repo, "jarvis-0.1.1",
                                   fetch=fetch, slug="o/r")
    assert out["from"] == "jarvis-0.1.0" and out["prs"] == [2, 3] and fetched == [2, 3]
    states = {i["pr_number"]: i["state"] for i in central.morpheus_items()}
    assert states == {2: "queued", 3: "monitoring"}
    assert central.get_state(morpheus.RELEASE_KEY) == "jarvis-0.1.1"
    # Idempotent: shipit and the daemon may both queue the same release
    again = morpheus.enqueue_release(central, cfg, release_repo, "jarvis-0.1.1",
                                     fetch=fetch, slug="o/r")
    assert again["queued"] == [] and again["already_queued"] == [2, 3]
    assert len(central.morpheus_items()) == 2


def test_one_unreadable_pr_does_not_drop_the_rest(central, cfg, release_repo):
    def fetch(slug, n):
        if n == 2:
            raise RuntimeError("gh broke")
        return pr(n)

    out = morpheus.enqueue_release(central, cfg, release_repo, "jarvis-0.1.1",
                                   fetch=fetch, slug="o/r")
    assert out["errors"] == ["#2: gh broke"] and len(out["queued"]) == 1


def test_the_daemon_backstop_queues_the_running_release_once(central, cfg, release_repo):
    _git(release_repo, "checkout", "-q", "jarvis-0.1.1")
    calls = []
    fetch = lambda s, n: calls.append(n) or pr(n)  # noqa: E731
    assert morpheus.ensure_release_queued(central, cfg, release_repo, fetch=fetch,
                                          slug="o/r")["release"] == "jarvis-0.1.1"
    assert morpheus.ensure_release_queued(central, cfg, release_repo, fetch=fetch,
                                          slug="o/r") is None
    assert calls == [2, 3]


def test_a_dev_checkout_on_a_branch_queues_nothing(central, cfg, release_repo):
    _git(release_repo, "commit", "-q", "--allow-empty", "-m", "after the tag")
    assert morpheus.ensure_release_queued(central, cfg, release_repo,
                                          fetch=lambda s, n: pr(n), slug="o/r") is None


# -- 3. the queue


def test_the_section_decides_the_starting_state(central, cfg, clock):
    script = morpheus.enqueue(central, cfg, pr(1), repo="o/r")
    hints = morpheus.enqueue(central, cfg, pr(2, body("Watch the inbox.")), repo="o/r")
    waived = morpheus.enqueue(central, cfg, pr(3, body("None — docs only")), repo="o/r")
    missing = morpheus.enqueue(central, cfg, pr(4, "## Summary\nx"), repo="o/r")
    assert (script["state"], script["script_source"], script["wo_id"]) == \
        ("monitoring", "pr", "wo-1234abcd")
    assert script["deadline_ts"] == TEST_EPOCH + 3 * DAY
    assert hints["state"] == "queued" and hints["hints"] == "Watch the inbox."
    assert waived["state"] == "validated" and "docs only" in waived["resolution"]
    assert missing["state"] == "queued"
    assert morpheus.enqueue(central, cfg, pr(1), repo="o/r") is None


def test_a_registered_script_starts_monitoring(central, cfg, clock):
    item = morpheus.enqueue(central, cfg, pr(2, body("Watch.")), repo="o/r")
    moved = morpheus.set_script(central, item["id"], "exit 0", "agent")
    assert (moved["state"], moved["script_source"], moved["next_check_ts"]) == \
        ("monitoring", "agent", TEST_EPOCH)
    with pytest.raises(ValueError):
        morpheus.set_script(central, item["id"], "  ", "agent")


def test_waive_and_requeue(central, cfg, clock):
    item = morpheus.enqueue(central, cfg, pr(), repo="o/r")
    central.update_morpheus_item(item["id"], state="failed", action="neo:4")
    assert morpheus.waive(central, item["id"], "fine")["state"] == "validated"
    clock.advance(DAY)
    again = morpheus.requeue(central, cfg, item["id"])
    assert (again["state"], again["action"], again["deadline_ts"]) == \
        ("monitoring", "", TEST_EPOCH + DAY + 3 * DAY)


# -- 4. the mechanical half


def _run(exit_code):
    return lambda script, cwd, timeout: morpheus.CheckResult(exit_code, f"said {exit_code}")


@pytest.mark.parametrize("code,state", [(0, "validated"), (1, "failed"), (75, "monitoring"),
                                        (None, "monitoring")])
def test_the_exit_code_is_the_verdict(central, cfg, tmp_path, code, state):
    item = morpheus.enqueue(central, cfg, pr(), repo="o/r", now=TEST_EPOCH)
    moved = morpheus.sweep(central, cfg, tmp_path, now=TEST_EPOCH, run=_run(code))
    after = central.morpheus_item(item["id"])
    assert after["state"] == state and moved[state] == 1
    assert after["checks"] == 1 and after["last_output"] == f"said {code}"
    if state == "monitoring":
        assert after["next_check_ts"] == TEST_EPOCH + cfg.check_every_minutes * 60


def test_a_script_is_not_run_before_it_is_due(central, cfg, tmp_path):
    morpheus.enqueue(central, cfg, pr(), repo="o/r", now=TEST_EPOCH)
    morpheus.sweep(central, cfg, tmp_path, now=TEST_EPOCH, run=_run(75))
    ran = []
    morpheus.sweep(central, cfg, tmp_path, now=TEST_EPOCH + 60,
                   run=lambda *a: ran.append(1) or morpheus.CheckResult(0, ""))
    assert ran == []


def test_not_yet_past_the_deadline_is_timedout(central, cfg, tmp_path):
    item = morpheus.enqueue(central, cfg, pr(), repo="o/r", now=TEST_EPOCH)
    morpheus.sweep(central, cfg, tmp_path, now=TEST_EPOCH + 3 * DAY + 1, run=_run(75))
    after = central.morpheus_item(item["id"])
    assert after["state"] == "timedout" and "never reached a verdict" in after["resolution"]


def test_a_queued_pr_that_never_got_a_script_times_out_too(central, cfg, tmp_path):
    item = morpheus.enqueue(central, cfg, pr(2, body("Watch.")), repo="o/r", now=TEST_EPOCH)
    morpheus.sweep(central, cfg, tmp_path, now=TEST_EPOCH + 3 * DAY + 1, run=_run(0))
    after = central.morpheus_item(item["id"])
    assert after["state"] == "timedout" and "no validation script" in after["resolution"]


def test_run_script_reads_the_real_exit_code_and_output(tmp_path):
    assert morpheus.run_script("echo hi; exit 1", tmp_path, 10) == \
        morpheus.CheckResult(1, "hi")
    timed = morpheus.run_script("sleep 5", tmp_path, 1)
    assert timed.exit is None and "timed out" in timed.output


# -- 5. failed and timed out must act


def test_failed_with_a_work_order_files_an_investigation_once(central, tmp_path):
    item = morpheus.enqueue(central, MorpheusConfig(), pr(), repo="o/r", now=TEST_EPOCH)
    central.update_morpheus_item(item["id"], state="failed", resolution="broken")
    asked = []
    action = morpheus.act(central, catalog(), central.morpheus_item(item["id"]),
                          investigate=lambda c, i: ("fo-inv00001", ""),
                          ask_neo=lambda *a: asked.append(1) or 9)
    assert action == "investigation:fo-inv00001" and asked == []
    assert central.morpheus_item(item["id"])["action"] == action
    assert morpheus.owed_actions(central) == []
    titles = [r["title"] for r in central.conn.execute("SELECT title FROM inbox")]
    assert titles == ["PR #7 failed production validation"]


def test_an_investigation_that_cannot_be_filed_asks_neo_instead(central):
    item = morpheus.enqueue(central, MorpheusConfig(), pr(), repo="o/r", now=TEST_EPOCH)
    central.update_morpheus_item(item["id"], state="failed")
    notes = []
    action = morpheus.act(central, catalog(), central.morpheus_item(item["id"]),
                          investigate=lambda c, i: ("", "the steward said no"),
                          ask_neo=lambda c, i, note: notes.append(note) or 9)
    assert action == "neo:9" and notes == ["the steward said no"]


def test_timedout_asks_neo_by_default_and_the_route_is_configurable(central):
    item = morpheus.enqueue(central, MorpheusConfig(), pr(), repo="o/r", now=TEST_EPOCH)
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
    item = morpheus.enqueue(central, MorpheusConfig(), pr(title="untitled"), repo="o/r")
    assert morpheus._investigate(catalog(), item) == \
        ("", "the PR names no work order to investigate")


def test_the_neo_question_is_a_validation_question_naming_the_item(central):
    from jarvis.neo_store import NeoStore

    item = morpheus.enqueue(central, MorpheusConfig(), pr(), repo="o/r")
    central.update_morpheus_item(item["id"], state="timedout", resolution="no verdict")
    qid = morpheus._ask_neo(catalog(), central.morpheus_item(item["id"]), "")
    neo = NeoStore()
    try:
        q = neo.get(qid)
    finally:
        neo.close()
    assert q["kind"] == "validation" and q["wo_id"] == ""
    assert morpheus.item_of_question(q) == item["id"]
    assert f"jarvis morpheus requeue {item['id']}" in q["question"]


@pytest.mark.parametrize("escalate,level", [(True, "warning"), (False, "info")])
def test_neo_s_verdict_is_recorded_and_reaches_the_inbox(central, escalate, level):
    item = morpheus.enqueue(central, MorpheusConfig(), pr(), repo="o/r")
    central.update_morpheus_item(item["id"], state="timedout", resolution="no verdict")
    q = {"id": 5, "project": "jarvis_os", "context": json.dumps({"morpheus_item": item["id"]})}
    morpheus.record_neo_verdict(central, q, {"escalate": escalate, "answer": "rare path",
                                             "reason": ""}, "http://x/neo/question/5")
    after = central.morpheus_item(item["id"])
    assert after["state"] == "timedout" and after["resolution"].endswith("Neo: rare path")
    rows = central.conn.execute("SELECT level, title FROM inbox").fetchall()
    assert [(r["level"], "PR #7" in r["title"]) for r in rows] == [(level, True)]


# -- 6. the only LLM: Morpheus's session


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
    morpheus.enqueue(central, MorpheusConfig(), pr(), repo="o/r")  # has its own script
    assert morpheus.wake(central, catalog(), tmp_path, now=TEST_EPOCH, call=turn) == "idle"
    assert turn.calls == []


def test_a_queued_pr_wakes_one_turn_that_spawns_subagents(central, tmp_path):
    turn = FakeTurn()
    item = morpheus.enqueue(central, MorpheusConfig(), pr(2, body("Watch the inbox.")),
                            repo="o/r")
    assert morpheus.wake(central, catalog(), tmp_path, now=TEST_EPOCH, call=turn) == "scripts"
    [call] = turn.calls
    assert call["resume"] is False and item["id"] in call["prompt"]
    assert "Watch the inbox." in call["prompt"]
    assert "spawn ONE subagent" in call["append_system_prompt"]
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
    morpheus.enqueue(central, MorpheusConfig(), pr(2, body("Watch.")), repo="o/r")
    morpheus.wake(central, catalog(), tmp_path, now=TEST_EPOCH, call=turn)
    assert morpheus.session_state(central)["compact_due"] is True
    assert morpheus.wake(central, catalog(), tmp_path, now=TEST_EPOCH + 60,
                         call=turn) == "compact"
    assert turn.calls[-1]["prompt"] == claude_cli.COMPACT_PROMPT and turn.calls[-1]["resume"]
    state = morpheus.session_state(central)
    assert state["compactions"] == 1 and state["compact_due"] is False


def test_under_the_share_nothing_compacts(central, tmp_path):
    turn = FakeTurn(share=0.29)
    morpheus.enqueue(central, MorpheusConfig(), pr(2, body("Watch.")), repo="o/r")
    morpheus.wake(central, catalog(), tmp_path, now=TEST_EPOCH, call=turn)
    assert morpheus.session_state(central)["compact_due"] is False


def test_a_pause_stops_the_session_and_nothing_else(central, tmp_path):
    turn = FakeTurn()
    morpheus.enqueue(central, MorpheusConfig(), pr(2, body("Watch.")), repo="o/r")
    assert morpheus.wake(central, catalog(), tmp_path, paused=True, now=TEST_EPOCH,
                         call=turn) == "paused"
    assert turn.calls == []


def test_a_failed_turn_is_recorded_and_still_counts_as_an_offer(central, tmp_path):
    turn = FakeTurn(fail=True)
    morpheus.enqueue(central, MorpheusConfig(), pr(2, body("Watch.")), repo="o/r")
    assert morpheus.wake(central, catalog(), tmp_path, now=TEST_EPOCH, call=turn) == "error"
    state = morpheus.session_state(central)
    assert state["last_error"] == "boom" and state["errors"] == 1
    assert morpheus.wake(central, catalog(), tmp_path, now=TEST_EPOCH + 60, call=turn) == "idle"


# -- 7. enforcing the section


def _repo_with_template(tmp_path: Path, template: str) -> Path:
    repo = tmp_path / "proj"
    (repo / ".github").mkdir(parents=True)
    (repo / ".git").mkdir()
    (repo / ".github/pull_request_template.md").write_text(template)
    (repo / "sub").mkdir()
    return repo


def test_the_section_is_required_only_where_the_template_asks_for_it(tmp_path):
    with_it = _repo_with_template(tmp_path / "a", f"## Summary\n\n## {morpheus.SECTION}\n")
    without = _repo_with_template(tmp_path / "b", "## Summary\n")
    assert hooks.validation_required(with_it / "sub") is True
    assert hooks.validation_required(without) is False
    assert hooks.validation_required(tmp_path) is False


def test_the_hook_denies_a_body_without_it(tmp_path):
    repo = _repo_with_template(tmp_path, (REPO_ROOT / ".github/pull_request_template.md")
                               .read_text())
    good = "\n\n".join(f"## {s}\nfilled" for s in hooks.PR_BODY_SECTIONS)
    (repo / "good.md").write_text(good + f"\n\n## {morpheus.SECTION}\nNone — docs only\n")
    (repo / "bad.md").write_text(good)
    payload = {"cwd": str(repo), "tool_input": {
        "command": "gh pr create --title '[wo-1234abcd] x' --body-file bad.md"}}
    env = {"JARVIS_WO_ID": "wo-1234abcd"}
    denied = hooks.pr_body_decision(payload, env)
    assert denied is not None and morpheus.SECTION in json.dumps(denied)
    payload["tool_input"]["command"] = payload["tool_input"]["command"].replace("bad", "good")
    assert hooks.pr_body_decision(payload, env) is None


def test_ci_check_script(tmp_path):
    script = REPO_ROOT / "scripts/check_pr_validation.py"
    ok = subprocess.run([sys.executable, str(script)], input=SCRIPT_BODY, text=True,
                        capture_output=True, env={"PATH": "/usr/bin:/bin"})
    assert ok.returncode == 0 and "script" in ok.stdout
    bad = subprocess.run([sys.executable, str(script)], text=True, capture_output=True,
                         env={"PATH": "/usr/bin:/bin", "PR_BODY": "## Summary\nx"})
    assert bad.returncode == 1 and "::error::" in bad.stdout


def test_the_workflow_reruns_on_an_edited_body():
    wf = (REPO_ROOT / ".github/workflows/pr-body.yml").read_text()
    assert "edited" in wf and "scripts/check_pr_validation.py" in wf


def test_the_skill_documents_the_section():
    skill = (REPO_ROOT / "src/jarvis/assets/skills/open-a-pull-request/SKILL.md").read_text()
    assert morpheus.SECTION in skill and "exit 1" in skill.lower()


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


def test_an_overdue_item_is_a_violation(central, cfg, monkeypatch):
    from jarvis import invariants

    monkeypatch.setattr(invariants, "_live_catalog", lambda: None)
    item = morpheus.enqueue(central, cfg, pr(), repo="o/r", now=TEST_EPOCH)
    monkeypatch.setattr("jarvis.db.now", lambda: TEST_EPOCH + 3 * DAY + 2 * 3600)
    [v] = list(invariants.check_morpheus_moves())
    assert v.invariant == "INV-MORPHEUS-OVERDUE" and v.subject_id == item["id"]
    assert invariants.check_morpheus_moves in invariants.OS_INVARIANTS


def test_an_owed_action_is_a_violation_and_a_taken_one_is_not(central, cfg, monkeypatch):
    from jarvis import invariants

    monkeypatch.setattr(invariants, "_live_catalog", lambda: None)
    item = morpheus.enqueue(central, cfg, pr(), repo="o/r", now=TEST_EPOCH)
    monkeypatch.setattr("jarvis.db.now", lambda: TEST_EPOCH)
    central.update_morpheus_item(item["id"], state="failed")
    monkeypatch.setattr("jarvis.db.now", lambda: TEST_EPOCH + 2 * 3600)
    assert [v.invariant for v in invariants.check_morpheus_moves()] == ["INV-MORPHEUS-OVERDUE"]
    central.update_morpheus_item(item["id"], action="neo:1")
    assert list(invariants.check_morpheus_moves()) == []


# -- 10. the daemon, the CLI and the dashboard


def test_the_daemon_pass_runs_due_scripts_and_acts(central, catalog_file, monkeypatch,
                                                   tmp_path):
    from jarvis.catalog import load_catalog
    from jarvis.daemon import Daemon

    daemon = Daemon(load_catalog(catalog_file))
    good = morpheus.enqueue(central, MorpheusConfig(), pr(1, body("```bash\nexit 0\n```")),
                            repo="o/r")
    bad = morpheus.enqueue(central, MorpheusConfig(), pr(2, body("```sh\nexit 1\n```"),
                                                         title="no wo"), repo="o/r")
    monkeypatch.setattr(morpheus, "_ask_neo", lambda c, i, note="": 42)
    daemon._morpheus_pass()
    assert central.morpheus_item(good["id"])["state"] == "validated"
    after = central.morpheus_item(bad["id"])
    assert after["state"] == "failed" and after["action"] == "neo:42"


def test_cli_status_and_script(central, cfg, tmp_path, capsys):
    from jarvis.cli import main

    item = morpheus.enqueue(central, cfg, pr(2, body("Watch.")), repo="o/r")
    (tmp_path / "s.sh").write_text("exit 75\n")
    assert main(["morpheus", "script", item["id"], "--file", str(tmp_path / "s.sh")]) == 0
    capsys.readouterr()
    assert main(["morpheus", "status", "--json"]) == 0
    rep = json.loads(capsys.readouterr().out)
    assert rep["counts"]["monitoring"] == 1 and rep["items"][0]["script"] == "exit 75"
    assert main(["morpheus", "show", "mv-nope"]) == 1


def test_the_dashboard_page(central, cfg):
    from fastapi.testclient import TestClient

    from jarvis.ui.app import create_app

    morpheus.enqueue(central, cfg, pr(), repo="o/r")
    page = TestClient(create_app()).get("/morpheus")
    assert page.status_code == 200
    assert "#7" in page.text and "monitoring" in page.text and 'href="/morpheus"' in page.text


def test_a_failed_first_turn_mints_a_fresh_session_next_time(central, tmp_path):
    turn = FakeTurn(fail=True)
    morpheus.enqueue(central, MorpheusConfig(), pr(2, body("Watch.")), repo="o/r")
    morpheus.wake(central, catalog(), tmp_path, now=TEST_EPOCH, call=turn)
    assert "session_id" not in morpheus.session_state(central)


def test_a_validation_question_cannot_be_answered_like_a_worker_question(central):
    from jarvis import ops

    item = morpheus.enqueue(central, MorpheusConfig(), pr(), repo="o/r")
    qid = morpheus._ask_neo(catalog(), item, "")
    with pytest.raises(ops.OpsError, match=f"jarvis morpheus waive {item['id']}"):
        ops.neo_answer_escalated(qid, "looks fine")
