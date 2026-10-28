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
    assert morpheus.act(central, catalog(), _failed(central),
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
    assert f'action="/morpheus/{item["id"]}/waive"' in page.text
    assert f'action="/morpheus/{item["id"]}/requeue"' in page.text
    assert "Send answer to worker" not in page.text

    r = client.post(f"/morpheus/{item['id']}/waive", follow_redirects=False,
                    data={"reason": " ", "next": "/neo#tab-escalated"})
    assert "error=" in r.headers["location"] and _status(qid) == "escalated"
    r = client.post(f"/morpheus/{item['id']}/waive", follow_redirects=False,
                    data={"reason": "works", "next": "/neo#tab-escalated"})
    assert r.headers["location"] == "/neo#tab-escalated"
    assert central.morpheus_item(item["id"])["state"] == "validated"
    assert _status(qid) == "answered"


# -- 6. the only LLM: Morpheus's session turns hints into scripts


class FakeTurn:
    def __init__(self, share: float = 0.1, fail: bool = False,
                 error: BaseException | None = None):
        self.calls: list[dict] = []
        self.share, self.fail, self.error = share, fail, error

    def __call__(self, prompt, session_id, **kw):
        self.calls.append({"prompt": prompt, "session_id": session_id, **kw})
        if self.error is not None:
            raise self.error
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


def test_a_turn_killed_before_it_answered_costs_no_offer(central, tmp_path):
    """The release restart SIGTERMs the turn it just enqueued work for (#1161)"""
    killed = claude_cli.KilledError(claude_cli.Killed(
        message="claude -p --output-format json... was killed by a signal (rc=143) "
                "and wrote nothing", rc=143))
    turn = FakeTurn(error=killed)
    queued(central, 2)
    assert morpheus.wake(central, catalog(), tmp_path, now=TEST_EPOCH, call=turn) == "killed"
    state = morpheus.session_state(central)
    assert state["kills"] == 1 and "rc=143" in state["last_error"]
    assert not state.get("offers") and not state.get("errors")
    # The next pass after the restart offers it again at once, not in REOFFER_SECONDS
    turn.error = None
    assert morpheus.wake(central, catalog(), tmp_path, now=TEST_EPOCH + 10,
                         call=turn) == "scripts"


def sigkilled(rc: int = 143) -> "claude_cli.KilledError":
    return claude_cli.KilledError(claude_cli.Killed(
        message=f"claude -p --output-format json... was killed by a signal (rc={rc}) "
                "and wrote nothing", rc=rc))


def kill_until_held(central, tmp_path, *, now: float = TEST_EPOCH) -> str:
    """MAX_KILLS kills in a row, each its own pass; returns the last pass's verdict"""
    turn = FakeTurn(error=sigkilled())
    verdict = ""
    for n in range(morpheus.MAX_KILLS):
        verdict = morpheus.wake(central, catalog(), tmp_path, now=now + n, call=turn)
    return verdict


def test_consecutive_kills_hold_the_session_instead_of_retrying_every_wake(central, tmp_path):
    queued(central, 2)
    assert kill_until_held(central, tmp_path) == "killed"
    state = morpheus.session_state(central)
    assert state["kills"] == morpheus.MAX_KILLS
    assert state["kill_hold_until"] == TEST_EPOCH + 2 + morpheus.KILL_HOLD_SECONDS
    assert f"held after {morpheus.MAX_KILLS} killed turns in a row" in state["last_error"]
    # Not one further turn is attempted while the hold stands
    turn = FakeTurn()
    assert morpheus.wake(central, catalog(), tmp_path, now=TEST_EPOCH + 60,
                         call=turn) == "kill_held"
    assert turn.calls == []
    act = morpheus.activity(central, now=TEST_EPOCH + 60)
    assert act["kill_hold_until"] == state["kill_hold_until"]
    assert act["kills"] == morpheus.MAX_KILLS and act["max_kills"] == morpheus.MAX_KILLS
    # And the user is told, not left with nothing happening
    titles = [i["title"] for i in central.unacked_inbox("warning")]
    assert f"Morpheus held after {morpheus.MAX_KILLS} killed turns in a row" in titles
    # The hold expires: one retry, and it holds again if that one dies too
    assert morpheus.wake(central, catalog(), tmp_path,
                         now=TEST_EPOCH + morpheus.KILL_HOLD_SECONDS + 3,
                         call=turn) == "scripts"


def test_an_answered_turn_resets_the_kill_counter(central, tmp_path):
    queued(central, 2)
    killer = FakeTurn(error=sigkilled())
    for n in range(morpheus.MAX_KILLS - 1):
        assert morpheus.wake(central, catalog(), tmp_path, now=TEST_EPOCH + n,
                             call=killer) == "killed"
    assert morpheus.wake(central, catalog(), tmp_path, now=TEST_EPOCH + 10,
                         call=FakeTurn()) == "scripts"
    assert morpheus.session_state(central)["kills"] == 0
    # So the next kill starts counting from one again: no hold
    queued(central, 3)
    assert morpheus.wake(central, catalog(), tmp_path,
                         now=TEST_EPOCH + morpheus.REOFFER_SECONDS, call=killer) == "killed"
    state = morpheus.session_state(central)
    assert state["kills"] == 1 and "kill_hold_until" not in state


def test_a_new_release_lifts_the_kill_hold(central, tmp_path):
    """A release restart is both the usual cause of the kills and the usual fix (#1161)"""
    central.set_state(morpheus.RELEASE_KEY, "jarvis-0.1.1")
    queued(central, 2)
    kill_until_held(central, tmp_path)
    assert morpheus.wake(central, catalog(), tmp_path, now=TEST_EPOCH + 60,
                         call=FakeTurn()) == "kill_held"
    central.set_state(morpheus.RELEASE_KEY, "jarvis-0.1.2")
    turn = FakeTurn()
    assert morpheus.wake(central, catalog(), tmp_path, now=TEST_EPOCH + 120,
                         call=turn) == "scripts"
    assert len(turn.calls) == 1
    assert morpheus.activity(central, now=TEST_EPOCH + 120)["kill_hold_until"] is None


def test_a_release_lifted_hold_starts_the_kill_count_again(central, tmp_path):
    """Decision #52 of 2026-10-22: one restart after the release must not re-arm the hold"""
    central.set_state(morpheus.RELEASE_KEY, "jarvis-0.1.1")
    queued(central, 2)
    kill_until_held(central, tmp_path)
    central.set_state(morpheus.RELEASE_KEY, "jarvis-0.1.2")
    # Lifted on a pass with nothing to do: the reset is kept all the same
    for item in central.conn.execute("SELECT id FROM morpheus_items").fetchall():
        central.conn.execute("UPDATE morpheus_items SET state='validated' WHERE id=?",
                             (item["id"],))
    assert morpheus.wake(central, catalog(), tmp_path, now=TEST_EPOCH + 60,
                         call=FakeTurn()) == "idle"
    assert morpheus.session_state(central)["kills"] == 0
    queued(central, 3)
    assert morpheus.wake(central, catalog(), tmp_path, now=TEST_EPOCH + 120,
                         call=FakeTurn(error=sigkilled())) == "killed"
    state = morpheus.session_state(central)
    assert state["kills"] == 1 and "kill_hold_until" not in state


def test_the_status_line_says_the_session_is_held_after_the_kills(central, tmp_path, capsys,
                                                                  clock):
    from jarvis.cli import main

    queued(central, 2)
    kill_until_held(central, tmp_path, now=db.now())
    assert main(["morpheus", "status"]) == 0
    out = capsys.readouterr().out
    assert "held until" in out and f"({morpheus.MAX_KILLS} killed turns in a row)" in out
    assert "or the next release" in out


def refused(reset_at: float | None) -> "claude_cli.UsageLimitError":
    return claude_cli.UsageLimitError(claude_cli.UsageLimit(
        message="You've hit your session limit · resets 6:20am (America/Los_Angeles)",
        reset_at=reset_at))


def test_a_turn_refused_by_the_usage_window_costs_no_offer_and_waits_for_the_reset(central,
                                                                                   tmp_path):
    """The model never answered: the same class as a kill (#1171)"""
    reset_at = TEST_EPOCH + 4000
    turn = FakeTurn(error=refused(reset_at))
    queued(central, 2)
    assert morpheus.wake(central, catalog(), tmp_path, now=TEST_EPOCH,
                         call=turn) == "usage_limit"
    state = morpheus.session_state(central)
    assert state["usage_limits"] == 1 and state["usage_hold_until"] == reset_at
    assert not state.get("offers") and not state.get("errors")
    assert morpheus.activity(central, now=TEST_EPOCH + 60)["usage_hold_until"] == reset_at
    # Not one turn is attempted while the window is spent, whatever is queued
    turn.error = None
    assert morpheus.wake(central, catalog(), tmp_path, now=reset_at - 1,
                         call=turn) == "usage_held"
    assert len(turn.calls) == 1
    # The first pass after the reset offers the same batch again
    assert morpheus.wake(central, catalog(), tmp_path, now=reset_at, call=turn) == "scripts"
    assert len(turn.calls) == 2
    assert morpheus.activity(central, now=reset_at)["usage_hold_until"] is None


def test_a_refusal_that_named_no_reset_holds_for_the_fallback_delay(central, tmp_path):
    from jarvis.worker_session import RATE_LIMIT_FALLBACK_DELAY

    queued(central, 2)
    morpheus.wake(central, catalog(), tmp_path, now=TEST_EPOCH, call=FakeTurn(error=refused(None)))
    state = morpheus.session_state(central)
    assert state["usage_hold_until"] == TEST_EPOCH + RATE_LIMIT_FALLBACK_DELAY


def test_the_status_line_says_the_session_is_held_by_the_usage_window(central, tmp_path, capsys,
                                                                     clock):
    from jarvis.cli import main

    queued(central, 2)
    morpheus.wake(central, catalog(), tmp_path, now=db.now(),
                  call=FakeTurn(error=refused(db.now() + 4000)))
    assert main(["morpheus", "status"]) == 0
    out = capsys.readouterr().out
    assert "(usage limit)" in out and "held until" in out


# -- 6b. the surfaces say what the session is DOING, not only what it is allowed to do (#1162)


def test_a_turn_in_flight_is_visible_while_it_runs_and_gone_after(central, tmp_path):
    seen = {}

    def turn(prompt, session_id, **kw):
        seen["activity"] = morpheus.activity(central, now=TEST_EPOCH + 5)
        return claude_cli.HeadlessResult(text="ok", usage={}, session_id=session_id,
                                         model="sonnet")

    queued(central, 2)
    morpheus.wake(central, catalog(), tmp_path, now=TEST_EPOCH, call=turn)
    assert seen["activity"]["turn_in_flight"] is True
    assert seen["activity"]["turn_started_ts"] == TEST_EPOCH
    after = morpheus.activity(central, now=TEST_EPOCH + 10)
    assert after["turn_in_flight"] is False and after["abandoned_turn_ts"] is None


def test_a_turn_marker_older_than_the_timeout_is_a_crash_not_work_in_flight(central, tmp_path):
    queued(central, 2)
    morpheus.wake(central, catalog(), tmp_path, now=TEST_EPOCH, call=FakeTurn(fail=True))
    central.set_state(morpheus.SESSION_KEY, json.dumps(
        {**morpheus.session_state(central), "turn_started_ts": TEST_EPOCH}))
    act = morpheus.activity(central, timeout=900, now=TEST_EPOCH + 901)
    assert act["turn_in_flight"] is False and act["abandoned_turn_ts"] == TEST_EPOCH


def test_a_queued_item_in_the_reoffer_backoff_says_when_and_why(central, tmp_path):
    item = queued(central, 2)
    morpheus.wake(central, catalog(), tmp_path, now=TEST_EPOCH, call=FakeTurn(fail=True))
    act = morpheus.activity(central, now=TEST_EPOCH + 60)
    assert act["due"] == 0 and act["turn_in_flight"] is False
    assert act["held"] == [{"id": item["id"], "offers": 1, "exhausted": False,
                            "cause": "error",
                            "next_offer_ts": TEST_EPOCH + morpheus.REOFFER_SECONDS}]
    for n in range(1, morpheus.MAX_OFFERS):
        morpheus.wake(central, catalog(), tmp_path, call=FakeTurn(fail=True),
                      now=TEST_EPOCH + n * morpheus.REOFFER_SECONDS + 1)
    spent = morpheus.activity(central, now=TEST_EPOCH + 10 * DAY)["held"]
    assert spent == [{"id": item["id"], "offers": morpheus.MAX_OFFERS, "exhausted": True,
                      "cause": "error", "next_offer_ts": None}]


def test_an_offer_entry_written_before_the_cause_was_recorded_claims_no_cause(central, capsys,
                                                                             clock):
    """Pre-#1161 entries were killed turns rendered as 'after an offer'"""
    from jarvis.cli import main

    item = queued(central, 2)
    central.set_state(morpheus.SESSION_KEY, json.dumps(
        {"offers": {item["id"]: {"n": 1, "ts": db.now()}}}))
    assert morpheus.activity(central, now=db.now() + 60)["held"][0]["cause"] is None
    assert main(["morpheus", "status"]) == 0
    out = capsys.readouterr().out
    assert "held back until" in out and "after an offer" not in out


def test_the_status_line_separates_the_pause_mode_from_the_activity(central, tmp_path, capsys,
                                                                    clock):
    from jarvis.cli import main

    item = queued(central, 2)
    assert main(["morpheus", "status"]) == 0
    out = capsys.readouterr().out
    assert "unpaused (follows the fleet) · idle · 1 queued due now" in out
    assert "running (follows the fleet" not in out
    morpheus.wake(central, catalog(), tmp_path, now=TEST_EPOCH, call=FakeTurn(fail=True))
    assert main(["morpheus", "status"]) == 0
    assert f"{item['id']} offered 1 of 3: held back after a failed turn until" \
        in capsys.readouterr().out


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


def test_the_dashboard_shows_the_activity_and_its_backoff_not_only_the_pause_mode(central,
                                                                                  tmp_path):
    """#1171: the pause mode read healthy while the usage window held every turn"""
    from fastapi.testclient import TestClient

    from jarvis.ui.app import create_app

    item = queued(central, 2)
    morpheus.wake(central, catalog(), tmp_path, now=db.now(),
                  call=FakeTurn(error=refused(db.now() + 4000)))
    page = TestClient(create_app()).get("/morpheus").text
    assert "unpaused (follows the fleet)" in page and "(usage limit)" in page
    # Nothing was offered, so the item is not held back on its own account
    assert f"{item['id']} offered" not in page


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
