"""Decision orders: the researcher's brief, fence, report and submission (docs/superpowers/specs/2026-10-31-decision-orders.md §3)"""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path

import pytest

from jarvis import cli, decision_report, dispatch, hooks, ops, supervisor, timeline
from jarvis.catalog import load_catalog
from jarvis.project_store import ProjectStore, decision_report_sha

REPORT = {
    "summary": "B is 30% cheaper at our volume",
    "findings": [{"key": "f1", "title": "cost", "detail": "A $10, B $7 per month",
                  "evidence": ["jarvis cost proj_a: $10.00"]}],
    "options": [{"key": "a", "title": "keep A", "pros": ["no work"], "cons": ["cost"],
                 "cost": "$10/mo"},
                {"key": "b", "title": "move to B", "pros": ["cheaper"], "cons": [],
                 "cost": "one day"}],
    "recommendation": {"option": "b", "why": "cheaper within a month"},
    "proposed_orders": [{"key": "p1", "as": "wo", "project": "", "title": "migrate",
                         "description": "move the cache to B"}],
}


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project, monkeypatch):
    monkeypatch.delenv("JARVIS_WO_ID", raising=False)
    ops.start_os(str(catalog_file), foreground=True)


@pytest.fixture()
def store(project):
    s = ProjectStore(project)
    yield s
    s.close()


@pytest.fixture()
def project_spec(catalog_file):
    return load_catalog(catalog_file).projects[0]


def _opened(store, catalog_file) -> dict:
    """A decision order with its researcher opened by the daemon"""
    from jarvis.daemon import Daemon

    do = ops.create_decision_order("proj_a", "which cache", "compare the two caches")
    daemon = Daemon(load_catalog(catalog_file))
    daemon.plan_features(daemon.catalog.projects[0], store)
    return store.get_feature_order(do["id"])


def _researchers(store) -> list[dict]:
    return [w for w in store.list_work_orders(limit=200) if w.get("kind") == "researcher"]


# -- opening it


def test_the_daemon_opens_one_researcher_with_the_ask_verbatim(started, store,
                                                               catalog_file):
    from jarvis.daemon import Daemon

    do = _opened(store, catalog_file)
    daemon = Daemon(load_catalog(catalog_file))
    daemon.plan_features(daemon.catalog.projects[0], store)  # a second tick files nothing
    opened = _researchers(store)
    assert len(opened) == 1, opened
    assert opened[0]["parent_id"] == do["id"] and opened[0]["origin"] == "os"
    assert opened[0]["description"] == "compare the two caches"
    assert do["status"] == "planning" and do["plan_wo_id"] == opened[0]["id"]
    # Kind filters are positive: no planner, analyst or investigator for this row
    assert not [w for w in store.list_work_orders(limit=200)
                if w["kind"] in ("planner", "analyst", "investigator")]


# -- the kind branches


def _prompt(store, kind: str, project_spec) -> str:
    wo = store.create_work_order("compare caches", description="the ask", kind=kind)
    feature = None
    if kind == "researcher":
        do = store.create_feature_order("which cache", description="the ask",
                                        kind="decision")
        store.update_work_order(wo["id"], parent_id=do["id"])
        wo = store.get_work_order(wo["id"])
        feature = {"fo": do, "children": []}
    return dispatch.build_worker_prompt(wo, project_spec, feature=feature)


def test_the_researcher_prompt_is_its_own_branch_and_not_the_worker_contract(
        store, project_spec):
    """kn-31f0f450: the worker control sits beside it"""
    researcher = _prompt(store, "researcher", project_spec)
    worker = _prompt(store, "worker", project_spec)
    assert "RESEARCHER for Jarvis decision order `do-" in researcher
    assert "jarvis do report do-" in researcher and "--from-file report.json" in researcher
    assert decision_report.SCHEMA_TEXT in researcher
    assert "The PR title MUST start with" in worker
    assert "The PR title MUST start with" not in researcher
    # Strip prohibitions, then nothing tells it to open a PR or finish by hand
    allowed = "\n".join(line for line in researcher.splitlines()
                        if not re.search(r"\b(never|not|no)\b", line.lower()))
    assert "gh pr create" not in allowed and "jarvis wo finish" not in allowed
    # The brief's order: the user decides, the ask, read-only, the report
    order = [researcher.index(s) for s in ("decision the USER will take", "the ask",
                                           "Read-only", "Deliver the report",
                                           "Present, never act")]
    assert order == sorted(order)
    assert "WHICH OPTION TO TAKE is the user's question" in researcher


def test_the_researcher_is_read_only_and_described_to_the_supervisor():
    assert "researcher" in dispatch.READ_ONLY_KINDS
    said = supervisor._what_it_is({"kind": "researcher", "parent_id": "do-1234abcd"})
    assert "RESEARCHER of decision order do-1234abcd" in said
    assert "NOT" in said and said != supervisor._what_it_is({"kind": "worker"})


def test_feature_context_carries_the_decision_row(store):
    do = store.create_feature_order("which cache", description="x", kind="decision")
    wo = store.create_work_order("r", kind="researcher", parent_id=do["id"])
    ctx = dispatch.feature_context(store, wo)
    assert ctx == {"fo": store.get_feature_order(do["id"]), "children": []}


# -- the fence


def _env(kind: str = "researcher") -> dict[str, str]:
    return {"JARVIS_WO_ID": "wo-1", hooks.WO_KIND_ENV: kind}


def _bash(command: str, cwd: str = "/tmp") -> dict:
    return {"tool_name": "Bash", "cwd": cwd, "tool_input": {"command": command}}


def _decision(result) -> str | None:
    return None if result is None else result["hookSpecificOutput"]["permissionDecision"]


SHIPS = [
    "git commit -m 'x'", "git push origin HEAD", "git -C /w commit -am x",
    "FOO=1 git push", "gh pr create --title t --body b", "gh pr merge 12 --squash",
    "cd /w && git commit -m x", "ls; git push",
    # Wrapped: one per wrapper form
    "env git push", "env -i FOO=1 git push", "env -u JARVIS_WO_ID git commit -m x",
    "command git push", "sudo -u root git push", "nohup git push", "time git commit -m x",
    "bash -c 'git push'", "sh -c 'cd /w && git commit -m x'", "zsh -lc \"gh pr create -t t\"",
    "env -i bash -c 'nohup git push'",
]
READS = [
    "git log --oneline -5", "git diff main", "gh pr view 12", "python3 scripts/measure.py",
    "jarvis cost proj_a", "jarvis wo ask wo-1 \"should I git commit this note?\"",
    "sqlite3 --version", "uv run python -c 'print(1)'",
]


def test_a_researcher_may_not_commit_push_or_open_a_pull_request():
    for command in SHIPS:
        assert _decision(hooks.researcher_bash_decision(_bash(command), _env())) == "deny", \
            command
        # Through the whole chain too, ahead of the gate
        assert _decision(hooks.preflight_decision(_bash(command), _env())) == "deny", command
        # Control: no other kind's behaviour changes
        assert hooks.researcher_bash_decision(_bash(command), _env("worker")) is None


def test_a_researcher_may_run_any_read_or_analysis_command():
    for command in READS + ["bash -c 'git log -1'", "env -u X git status"]:
        assert hooks.researcher_bash_decision(_bash(command), _env()) is None, command


CLOSES = ["jarvis do done do-1", "jarvis do cancel do-1",
          "env -u JARVIS_WO_ID jarvis do done do-1", "env -i jarvis do cancel do-1",
          "command jarvis do done do-1", "sudo jarvis do done do-1", "nohup jarvis do done do-1",
          "time jarvis do done do-1", "bash -c 'jarvis do done do-1'",
          "ls && sh -c \"env -u JARVIS_WO_ID jarvis do cancel do-1\""]


def test_no_work_order_session_may_close_a_decision_order():
    """The model can strip JARVIS_WO_ID from its shell, not from the hook's env (§2)"""
    for kind in ("researcher", "worker", "investigator"):
        for command in CLOSES:
            refusal = hooks.preflight_decision(_bash(command), _env(kind))
            assert _decision(refusal) == "deny", (kind, command)
            assert "user's verb" in refusal["hookSpecificOutput"]["permissionDecisionReason"]
    # Controls: the user's own session, and the verbs that do not close
    for command in CLOSES:
        assert hooks.decision_close_decision(_bash(command), {}) is None
    for command in ("jarvis do show do-1", "jarvis do report do-1 --from-file report.json",
                    "jarvis wo done wo-1"):
        assert hooks.decision_close_decision(_bash(command), _env()) is None, command


def _write(path: str, cwd: str, tool: str = "Write") -> dict:
    return {"tool_name": tool, "cwd": cwd, "tool_input": {"file_path": path}}


def test_a_researcher_writes_only_its_report_and_its_scratch_tree(tmp_path):
    cwd = str(tmp_path / "wt")
    temp = hooks.researcher_temp_dir("wo-1")
    for ok in (f"{cwd}/report.json", f"{cwd}/.jarvis/scratch/notes.md",
               f"{cwd}/.jarvis/scratch/a/b.py", ".jarvis/scratch/rel.md", f"{temp}/m.py"):
        assert hooks.researcher_write_decision(_write(ok, cwd), _env()) is None, ok
    (tmp_path / "wt" / ".jarvis" / "scratch").mkdir(parents=True)
    (tmp_path / "wt" / ".jarvis" / "scratch" / "out").symlink_to(tmp_path / "other_project")
    bad = [f"{cwd}/src/app.py", f"{cwd}/.jarvis/state.db", f"{cwd}/sub/report.json",
           str(Path.home() / ".claude" / "settings.json"),
           str(tmp_path / "other_project" / "x.py"),
           f"{cwd}/.jarvis/scratch/out/x.py",  # symlink out of scratch
           f"{cwd}/.jarvis/scratch/../../x.py", "/elsewhere/x.py",
           f"{hooks.researcher_temp_dir('wo-2')}/m.py"]  # another researcher's temp dir
    for tool in ("Write", "Edit", "NotebookEdit"):
        for path in bad:
            payload = _write(path, cwd, tool)
            assert _decision(hooks.researcher_write_decision(payload, _env())) == "deny", path
            assert hooks.researcher_write_decision(payload, _env("worker")) is None
        # Missing cwd or file path fails CLOSED
        for payload in ({"tool_name": tool, "tool_input": {"file_path": f"{cwd}/report.json"}},
                        {"tool_name": tool, "cwd": cwd, "tool_input": {}}):
            assert _decision(hooks.researcher_write_decision(payload, _env())) == "deny"
    # Reachable through the chain, ahead of the worktree auto-allow
    settings = _write(str(Path.home() / ".claude" / "settings.json"), cwd, "Edit")
    assert _decision(hooks.preflight_decision(settings, _env())) == "deny"


def test_the_prompt_and_the_refusal_name_the_same_temp_dir(store, project_spec):
    researcher = _prompt(store, "researcher", project_spec)
    wo_id = re.search(r"running as work order `(wo-[0-9a-f]+)`", researcher).group(1)
    temp = str(hooks.researcher_temp_dir(wo_id))
    assert f"`{temp}/`" in researcher and "outside the worktree." not in researcher
    refusal = hooks.researcher_write_decision(_write("/elsewhere/x", "/w"),
                                              {**_env(), "JARVIS_WO_ID": wo_id})
    assert f"`{temp}/`" in refusal["hookSpecificOutput"]["permissionDecisionReason"]


def test_every_command_a_refusal_suggests_is_accepted_by_the_same_hook(tmp_path):
    """kn-30e15974 item 2: a refusal's remedy must clear its own kind's hook"""
    cwd = str(tmp_path)
    refusals = [hooks.researcher_bash_decision(_bash("git push"), _env()),
                hooks.researcher_write_decision(_write(f"{cwd}/src/x.py", cwd), _env())]
    for refusal in refusals:
        # The suggestions, not the "Refused: `<what>`" head that names the refused verb
        reason = refusal["hookSpecificOutput"]["permissionDecisionReason"].split(". ", 1)[1]
        commands = [c for c in re.findall(r"`([^`]+)`", reason)
                    if c.startswith(("jarvis", "git", "gh"))]
        assert commands, reason
        for command in commands:
            command = command.replace("<do-id>", "do-1234abcd")
            assert hooks.researcher_bash_decision(_bash(command, cwd), _env()) is None, \
                command
        for target in (hooks.RESEARCHER_REPORT, f"{hooks.RESEARCHER_SCRATCH}/notes.md"):
            assert f"`{target.split('/notes.md')[0]}" in reason
            assert hooks.researcher_write_decision(
                _write(f"{cwd}/{target}", cwd), _env()) is None


# -- the report module


def test_a_good_report_parses_and_normalises():
    parsed = decision_report.parse_report({**REPORT, "extra": 1})
    assert "extra" not in parsed
    assert parsed["recommendation"] == REPORT["recommendation"]
    minimal = decision_report.parse_report(
        {"summary": "s", "findings": [{"key": "f", "title": "t", "detail": "d"}],
         "recommendation": {"why": "w"}})
    assert minimal["options"] == [] and minimal["proposed_orders"] == []


def test_every_problem_is_reported_at_once():
    bad = copy.deepcopy(REPORT)
    bad["summary"] = "x" * (decision_report.MAX_SUMMARY_CHARS + 1)
    bad["findings"].append({"key": "f1", "title": "", "detail": "d"})
    bad["options"][0]["key"] = "Bad Key"
    bad["recommendation"]["option"] = "zz"
    bad["proposed_orders"][0]["as"] = "pr"
    bad["proposed_orders"] += [dict(bad["proposed_orders"][0], key=f"p{i}")
                               for i in range(2, 12)]
    with pytest.raises(decision_report.DecisionReportError) as caught:
        decision_report.parse_report(bad)
    text = "\n".join(caught.value.problems)
    for needle in ("`summary` is", "duplicate key 'f1'", "`title` is required",
                   "lowercase slug", "`recommendation.option`", "`as` must be one of",
                   "over the cap of 10"):
        assert needle in text, needle
    for doc in ([], {"summary": "s", "findings": [], "recommendation": {"why": "w"}},
                {"summary": "s", "findings": [{"key": "f", "title": "t", "detail": "d"}]}):
        with pytest.raises(decision_report.DecisionReportError):
            decision_report.parse_report(doc)


def test_render_and_headline_lead_with_the_answer():
    text = decision_report.render_report(REPORT)
    assert text.startswith(REPORT["summary"])
    assert "Recommendation: b: move to B" in text and "move to B (recommended)" in text
    assert "Proposed follow-up orders (1), yours to file:" in text
    assert decision_report.render_report({}) == "no report yet"
    line = decision_report.headline({"id": "do-1"}, REPORT)
    assert line == "do-1 report ready — 1 finding, 2 options; recommends b: move to B"
    assert "\n" not in line


# -- submission


def test_submission_stores_the_report_and_never_moves_the_order(started, store,
                                                                 catalog_file):
    do = _opened(store, catalog_file)
    researcher = do["plan_wo_id"]
    store.set_status(researcher, "running")
    out = ops.submit_decision_report(do["id"], REPORT)
    row = store.get_feature_order(do["id"])
    assert row["status"] == "planning" == out["status"]
    assert not row.get("needs_attention")
    stored = json.loads(row["plan"])
    assert stored["summary"] == REPORT["summary"]
    assert out["sha"] == decision_report_sha(row["plan"])
    events = store.events_of_kind(researcher, ops.DECISION_REPORT_EVENT)
    assert len(events) == 1
    payload = events[0]["payload"]
    payload = json.loads(payload) if isinstance(payload, str) else payload
    assert payload == {"do_id": do["id"], "sha": out["sha"]}
    # The researcher settled through finish (it does not stay running)
    assert store.get_work_order(researcher)["status"] != "running"
    label, _ = timeline._describe(ops.DECISION_REPORT_EVENT,
                                  {"do_id": do["id"], "sha": out["sha"]})
    assert label == f"Decision report submitted for {do['id']}"


def test_a_revision_is_accepted_while_awaiting_the_decision(started, store, catalog_file):
    do = _opened(store, catalog_file)
    store.set_feature_status(do["id"], "awaiting_decision")
    revised = {**REPORT, "summary": "B is 35% cheaper"}
    ops.submit_decision_report(do["id"], revised)
    row = store.get_feature_order(do["id"])
    assert row["status"] == "awaiting_decision"
    assert json.loads(row["plan"])["summary"] == "B is 35% cheaper"


def test_a_bad_report_stores_nothing_and_names_every_problem(started, store,
                                                             catalog_file):
    do = _opened(store, catalog_file)
    with pytest.raises(ops.OpsError) as caught:
        ops.submit_decision_report(do["id"], {"summary": "", "findings": []})
    assert "nothing was stored" in str(caught.value)
    assert "`summary` is required" in str(caught.value)
    assert not store.get_feature_order(do["id"])["plan"]


def test_submission_is_refused_outside_its_window_and_on_another_kind(started, store):
    do = ops.create_decision_order("proj_a", "which cache", "compare")  # still pending
    with pytest.raises(ops.OpsError, match="takes no report"):
        ops.submit_decision_report(do["id"], REPORT)
    inv = ops.create_feature_order("proj_a", "CSV export", description=(
        "Add a CSV exporter to the reporting module, with a command that calls it and "
        "tests over the happy path and an empty result set."))
    with pytest.raises(ops.OpsError, match="not a decision order"):
        ops.submit_decision_report(inv["id"], REPORT)


def test_the_cli_submits_from_a_file_and_from_stdin(started, store, catalog_file,
                                                    tmp_path, capsys, monkeypatch):
    import io

    do = _opened(store, catalog_file)
    path = tmp_path / "report.json"
    path.write_text(json.dumps(REPORT))
    assert cli.main(["do", "report", do["id"], "--from-file", str(path), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["do_id"] == do["id"]
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(REPORT)))
    assert cli.main(["do", "report", do["id"], "--from-file", "-", "--json"]) == 0
    capsys.readouterr()
    path.write_text("{not json")
    assert cli.main(["do", "report", do["id"], "--from-file", str(path)]) != 0
    assert cli.main(["do", "show", do["id"]]) == 0
    assert "Recommendation: b: move to B" in capsys.readouterr().out


# -- Neo


def test_a_researcher_question_carries_the_escalate_the_choice_rule(started, store,
                                                                     catalog_file):
    from jarvis.neo import RESEARCHER_RULE
    from jarvis.neo_store import NeoStore

    do = _opened(store, catalog_file)
    store.set_status(do["plan_wo_id"], "running")
    worker = store.create_work_order("w", kind="worker")
    store.set_status(worker["id"], "running")
    asked = ops.ask_question(do["plan_wo_id"], "where does the cost ledger live?")
    control = ops.ask_question(worker["id"], "CSV or JSON?")
    neo = NeoStore()
    try:
        assert neo.get(asked["question_id"])["context"].startswith(RESEARCHER_RULE)
        assert RESEARCHER_RULE not in neo.get(control["question_id"])["context"]
    finally:
        neo.close()
