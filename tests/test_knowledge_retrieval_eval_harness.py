"""The knowledge-retrieval eval must not be able to grade nothing"""

from __future__ import annotations

import argparse
import ast
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

from jarvis import catalog, claude_cli
from jarvis.central_store import CentralStore, headline

REPO_ROOT = Path(__file__).resolve().parents[1]
EVAL_PATH = REPO_ROOT / "evals" / "llm" / "test_knowledge_retrieval.py"

ENV_VAR = "JARVIS_EVALS_LLM"


MODULE_NAME = "_knowledge_retrieval_under_test"


@pytest.fixture(scope="module")
def eval_module():
    """The eval imported as a library, so its batteries can be inspected for free"""
    spec = importlib.util.spec_from_file_location(MODULE_NAME, EVAL_PATH)
    assert spec and spec.loader, f"cannot load {EVAL_PATH}"
    mod = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = mod
    try:
        spec.loader.exec_module(mod)
    except Exception:  # pragma: no cover — only on a broken eval
        sys.modules.pop(MODULE_NAME, None)
        raise
    yield mod
    sys.modules.pop(MODULE_NAME, None)


def test_the_eval_file_exists() -> None:
    assert EVAL_PATH.is_file(), (
        "the behavioural retrieval eval is gone — the index design has no measurement")


# -- the gate and the batteries


def test_the_skip_gate_is_spelled_exactly_right() -> None:
    """A misspelled gate variable skips for ever and reads as 'opt-in', not as broken"""
    tree = ast.parse(EVAL_PATH.read_text())
    names = {node.value for node in ast.walk(tree)
             if isinstance(node, ast.Constant) and isinstance(node.value, str)
             and node.value.startswith("JARVIS_EVALS")}
    assert ENV_VAR in names, (
        f"the eval's skip gate no longer reads {ENV_VAR} — it can never be switched on")


def test_the_batteries_are_not_empty(eval_module) -> None:
    """An emptied battery is the quiet way an eval stops measuring"""
    assert len(eval_module.CASES) >= 5, "the case battery has shrunk"
    assert len(eval_module.CONTROL) >= 2, (
        "fewer than two control cases — the tripwire that makes `applies` mean "
        "something is nearly gone")
    assert eval_module.NOISE, "no filler entries: the index is not an index to search"
    assert eval_module.PINNED, "no pinned entries: the pinned-vs-indexed distinction "\
        "the eval is sensitive to is untested"


def test_every_control_case_is_a_real_case(eval_module) -> None:
    """`CONTROL` is a list of names, so a rename silently empties the control battery rather than failing..."""
    known = {c.name for c in eval_module.CASES}
    unknown = [n for n in eval_module.CONTROL if n not in known]
    assert not unknown, (
        f"CONTROL names cases that do not exist: {unknown} — the blind run grades "
        f"nothing and `retrieval-llm/control` passes vacuously")


def test_case_names_are_unique(eval_module) -> None:
    """Runs are keyed by name in a dict; a duplicate silently drops a case"""
    names = [c.name for c in eval_module.CASES]
    assert len(names) == len(set(names)), f"duplicate case names in CASES: {names}"


# -- the cases are still rigged


def test_the_deciding_text_is_never_on_the_headline(eval_module) -> None:
    """The property that makes each case a lookup instead of a freebie"""
    for case in eval_module.CASES:
        first, _, rest = case.learning.partition("\n")
        assert rest.strip(), (
            f"{case.name}: the entry is a single line, so all of it is headline — "
            f"there is nothing left to retrieve")
        assert headline(case.learning).startswith(headline(first)[:40]), (
            f"{case.name}: headline() does not render the first line as expected")


def test_the_headline_names_the_area_without_naming_the_answer(eval_module) -> None:
    """A headline has to be aimable and unhelpful at once"""
    tells = {
        "deploy-pre-step": ["vendor-freeze"],
        "migration-down-file": ["_down.sql"],
        "reports-endpoint-timeout": ["timeout=90", "90"],
        "staging-hostname": ["staging-2"],
        "user-facing-error-style": ["ReportError", "lowercase"],
    }
    for case in eval_module.CASES:
        head = headline(case.learning)
        leaked = [t for t in tells.get(case.name, []) if t in head]
        assert not leaked, (
            f"{case.name}: {leaked} is on the INDEX headline, so the subject never has "
            f"to retrieve anything — the case grades a read, not a lookup")


def _subcommands_action(parser: argparse.ArgumentParser) -> argparse._SubParsersAction:
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    raise AssertionError(f"{parser.prog} has no subcommands")


def _subcommands(parser: argparse.ArgumentParser) -> set[str]:
    return set(_subcommands_action(parser).choices)


def test_read_verbs_are_real_learn_subcommands(eval_module) -> None:
    """Grading keys on `learn <verb>` being a read"""
    from jarvis.cli import build_parser

    learn = _subcommands(_subcommands_action(build_parser()).choices["learn"])
    assert learn, "could not introspect `jarvis learn` subcommands"
    missing = eval_module.READ_VERBS - learn
    assert not missing, (
        f"READ_VERBS names verbs `jarvis learn` no longer has: {missing} — retrieval "
        f"would score 0 and look like a behavioural regression")
    assert "add" not in eval_module.READ_VERBS, (
        "`add` is a write; counting it as retrieval would score a worker that only "
        "ever contributed to the base as one that read it")


# -- the predicates discriminate


def _lay_out(case, root: Path) -> Path:
    project = root / "project"
    for rel, body in case.files.items():
        path = project / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    return project


def test_no_case_is_already_applied_by_its_own_sandbox(eval_module, tmp_path) -> None:
    """The starting tree must not already satisfy the predicate"""
    for case in eval_module.CASES:
        project = _lay_out(case, tmp_path / f"pre-{case.name}")
        assert not case.applied(project), (
            f"{case.name}: applied() is already true on the untouched sandbox — this "
            f"case grades nothing and breaks the control battery")


#: The change each entry actually asks for, applied by hand
def _do_deploy(project: Path) -> None:
    (project / "Makefile").write_text(
        (project / "Makefile").read_text()
        + "\ndeploy-staging:\n\tmake vendor-freeze\n\t./push.sh staging\n")


def _do_migration(project: Path) -> None:
    (project / "migrations" / "002_archived_at_up.sql").write_text(
        "ALTER TABLE reports ADD COLUMN archived_at TIMESTAMP;\n")
    (project / "migrations" / "002_archived_at_down.sql").write_text(
        "ALTER TABLE reports DROP COLUMN archived_at;\n")


def _do_timeout(project: Path) -> None:
    (project / "client.py").write_text(
        (project / "client.py").read_text()
        + '\n\ndef fetch_report_summary(report_id):\n'
        '    return http_client.get(f"{BASE}/reports/summary?id={report_id}", '
        'timeout=90)\n')


def _do_staging(project: Path) -> None:
    (project / "config.py").write_text(
        (project / "config.py").read_text()
        + 'STAGING_URL = "https://staging-2.reports.internal"\n')


def _do_error_style(project: Path) -> None:
    (project / "reports.py").write_text(
        'from errors import ReportError\n\n'
        'REPORTS: dict[str, dict] = {}\n\n\n'
        'def load_report(report_id: str):\n'
        '    if report_id not in REPORTS:\n'
        '        raise ReportError(f"no report with id {report_id}")\n'
        '    return REPORTS[report_id]\n')


FIXES = {
    "deploy-pre-step": _do_deploy,
    "migration-down-file": _do_migration,
    "reports-endpoint-timeout": _do_timeout,
    "staging-hostname": _do_staging,
    "user-facing-error-style": _do_error_style,
}


def test_every_predicate_says_yes_to_the_change_its_entry_asks_for(
        eval_module, tmp_path) -> None:
    """The other half of discrimination, and the half nothing else would catch"""
    assert set(FIXES) == {c.name for c in eval_module.CASES}, (
        "a case was added or renamed without a hand-written fix here, so its predicate "
        "is no longer proved to discriminate")
    for case in eval_module.CASES:
        project = _lay_out(case, tmp_path / f"post-{case.name}")
        FIXES[case.name](project)
        assert case.applied(project), (
            f"{case.name}: applied() rejects the change its own knowledge entry asks "
            f"for — `applies` can never pass and `control` can never fail")


def test_the_error_style_predicate_rejects_the_guessable_half(
        eval_module, tmp_path) -> None:
    """`ReportError` is readable off errors.py; the message style is not"""
    case = next(c for c in eval_module.CASES if c.name == "user-facing-error-style")
    project = _lay_out(case, tmp_path / "half")
    (project / "reports.py").write_text(
        'from errors import ReportError\n\n'
        'REPORTS: dict[str, dict] = {}\n\n\n'
        'def load_report(report_id: str):\n'
        '    if report_id not in REPORTS:\n'
        '        raise ReportError("Report not found.")\n'
        '    return REPORTS[report_id]\n')
    assert not case.applied(project), (
        "the right exception with the wrong message style scores as applied — the case "
        "is grading what a subject can read off errors.py, not what it retrieved")


# -- the subject is configured like a real worker


def _headless_calls(source: str) -> list[dict[str, ast.expr]]:
    """Keyword arguments of every `run_headless(...)` in the eval"""
    tree = ast.parse(source)
    return [{kw.arg: kw.value for kw in node.keywords if kw.arg}
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and getattr(node.func, "attr", None) == "run_headless"]


def test_both_subject_runs_are_tooled_and_permissioned() -> None:
    """A subject without tools cannot retrieve"""
    calls = _headless_calls(EVAL_PATH.read_text())
    assert len(calls) == 2, (
        f"expected exactly two run_headless calls (graded + control), found "
        f"{len(calls)}")
    for kwargs in calls:
        for name in ("tools", "permission_mode", "env_extra", "cwd", "system_prompt"):
            assert name in kwargs, (
                f"a run_headless call in the eval no longer passes {name}=")
        assert isinstance(kwargs["permission_mode"], ast.Name) and \
            kwargs["permission_mode"].id == "PERMISSION_MODE"
        assert isinstance(kwargs["tools"], ast.Name) and kwargs["tools"].id == "TOOLS"


def test_the_subject_runs_the_permission_mode_production_dispatches(eval_module) -> None:
    """`auto` here is the production configuration, not an escalation of it"""
    assert eval_module.PERMISSION_MODE == catalog.DEFAULT_PERMISSION_MODE, (
        f"the eval runs its subject in {eval_module.PERMISSION_MODE!r} but dispatch "
        f"gives workers {catalog.DEFAULT_PERMISSION_MODE!r} — the subject is no longer "
        f"configured like the thing being measured")
    assert "Bash" in eval_module.TOOLS, "the subject cannot run `jarvis` at all"


def test_run_headless_forwards_permission_mode_and_env_to_the_cli(monkeypatch) -> None:
    """The plumbing, driven for real with only the model call stubbed"""
    argv: list[str] = []
    passed_env: list[dict[str, str] | None] = []

    def fake_run(args, cwd=None, timeout=None, env_extra=None, **kw):
        argv[:] = args
        passed_env.append(env_extra)
        return '{"result": "ok"}'

    monkeypatch.setattr(claude_cli, "_run", fake_run)
    out = claude_cli.run_headless(
        "task", system_prompt="contract", model="sonnet", tools="Bash,Read",
        permission_mode="auto", env_extra={"PATH": "/sandbox/bin", "JARVIS_HOME": "/s"})

    assert out == "ok"
    args = argv
    assert "--permission-mode" in args, (
        "run_headless dropped permission_mode — the eval's subject would hit a "
        "permission prompt it cannot answer and score as never retrieving")
    assert args[args.index("--permission-mode") + 1] == "auto"
    assert passed_env == [{"PATH": "/sandbox/bin", "JARVIS_HOME": "/s"}], (
        "run_headless dropped env_extra — the subject's `jarvis` would resolve to the "
        "developer's install against the developer's store")


def test_a_measured_subject_is_still_charged_to_the_work_order(monkeypatch) -> None:
    """`env_extra` controls the SUBJECT's environment; attribution reads the HARNESS's (#109)"""
    recorded: list[dict] = []

    monkeypatch.setattr(claude_cli, "_run",
                        lambda *a, **kw: '{"result": "ok", "modelUsage": {}}')
    monkeypatch.setenv("JARVIS_WO_ID", "wo-eval-kb")
    monkeypatch.setenv("JARVIS_PROJECT", "jarvis_os")
    home_before = os.environ.get("JARVIS_HOME")

    claude_cli.run_headless(
        "task", model="sonnet", tools="Bash,Read", permission_mode="auto",
        env_extra={"PATH": "/sandbox/bin", "JARVIS_HOME": "/sandbox/home"},
        record=lambda *a, **kw: recorded.append(kw))

    assert recorded, (
        "a headless call made inside a work order was not attributed to it — the "
        "retrieval eval is the priciest battery in the repo and would report as free")
    assert recorded[0]["wo_id"] == "wo-eval-kb"
    assert os.environ.get("JARVIS_HOME") == home_before, (
        "env_extra leaked into the calling process — the eval's cost records would be "
        "written to the sandbox store and thrown away with it")


# -- the apparatus works


def test_the_shim_reaches_the_real_cli_and_logs_what_it_was_asked(
        eval_module, tmp_path) -> None:
    """The grading instrument, end to end, with no model"""
    home = tmp_path / "home"
    home.mkdir()
    central = CentralStore(home / "os.db")
    row = central.add_knowledge(
        "staging moved\nStaging moved to https://staging-2.reports.internal in June.",
        project=eval_module.PROJECT, topic="environments")
    central.close()

    project, log, env = eval_module._sandbox(
        tmp_path / "root", eval_module.CASES[0], home)

    result = subprocess.run(
        ["jarvis", "learn", "show", row["id"]],
        cwd=project, capture_output=True, text=True,
        env={**os.environ, **env, "PYTHONPATH": str(REPO_ROOT / "src")})

    assert result.returncode == 0, f"shim failed: {result.stderr}"
    assert "staging-2.reports.internal" in result.stdout, (
        "retrieval through the shim did not return the entry's full text — the eval "
        "cannot tell a lookup from a miss")

    calls = eval_module._read_log(log)
    assert calls, "the shim logged nothing — every case would score as no retrieval"
    ts, argv = calls[0]
    assert argv[:2] == ["learn", "show"] and ts > 0, (
        f"the call log does not carry a timestamped argv: {calls[0]!r}")


def test_non_learn_subcommands_are_acknowledged_not_run(eval_module, tmp_path) -> None:
    """A subject that tries `jarvis wo finish` must not burn its run in a retry loop against a work order..."""
    home = tmp_path / "home"
    home.mkdir()
    CentralStore(home / "os.db").close()
    project, log, env = eval_module._sandbox(
        tmp_path / "root", eval_module.CASES[0], home)

    result = subprocess.run(
        ["jarvis", "wo", "finish", "wo-eval-kb"],
        cwd=project, capture_output=True, text=True,
        env={**os.environ, **env, "PYTHONPATH": str(REPO_ROOT / "src")})

    assert result.returncode == 0, "a non-learn subcommand must not fail the subject"
    assert "(sandbox) noted" in result.stdout
    assert eval_module._read_log(log), (
        "the shim must log every call, not just the ones it forwards — `bulk_dumped` "
        "and the call counts in the control breakdown read the whole log")


def test_retrieval_is_graded_against_the_subjects_own_store(eval_module, tmp_path) -> None:
    """The sandbox home is handed to the subject through `env_extra`, never exported"""
    home = tmp_path / "home"
    home.mkdir()
    _, _, env = eval_module._sandbox(tmp_path / "root", eval_module.CASES[0], home)

    assert env["JARVIS_HOME"] == str(home)
    assert os.environ.get("JARVIS_HOME") != str(home), (
        "the eval exported the sandbox home into the harness process — the runner and "
        "the subject are now sharing a store")
    assert str(tmp_path / "root" / "bin") in env["PATH"].split(os.pathsep), (
        "the shim's directory is not first on the subject's PATH; `jarvis` would "
        "resolve to the developer's real install")


def test_the_bulk_dump_detector_recognises_the_habit_the_index_replaced(
        eval_module) -> None:
    """`retrieval-llm/bounded` asserts an emptiness"""
    def run(*calls):
        return eval_module.Run(
            case=eval_module.CASES[0],
            calls=[(1.0, list(c)) for c in calls],
            first_mutation=None, reply="", root=Path("/nonexistent"))

    assert run(["learn", "list", "--full"]).bulk_dumped
    assert run(["learn", "list", "--limit", "500"]).bulk_dumped
    assert run(["learn", "show"] + [f"kn-{i}" for i in range(20)]).bulk_dumped

    assert not run(["learn", "search", "deploy"]).bulk_dumped
    assert not run(["learn", "show", "kn-1"]).bulk_dumped
    assert not run(["learn", "list", "--limit", "20"]).bulk_dumped
    assert not run(["learn", "topics"]).bulk_dumped


def test_a_lookup_after_the_first_write_does_not_count_as_looking_first(
        eval_module) -> None:
    """`before-acting` is a timestamp comparison between two different clocks' readings of the same clock..."""
    def run(read_at, mutated_at):
        return eval_module.Run(
            case=eval_module.CASES[0], calls=[(read_at, ["learn", "show", "kn-1"])],
            first_mutation=mutated_at, reply="", root=Path("/nonexistent"))

    assert run(10.0, 20.0).retrieved_before_acting, "read then wrote: this is a pass"
    assert not run(20.0, 10.0).retrieved_before_acting, (
        "wrote then read is a worker checking its homework, not one informed by the "
        "knowledge base")
    assert run(10.0, None).retrieved_before_acting, (
        "never wrote a file: there is nothing to have been late for")

    never_looked = eval_module.Run(
        case=eval_module.CASES[0], calls=[(5.0, ["wo", "show", "wo-eval-kb"])],
        first_mutation=None, reply="", root=Path("/nonexistent"))
    assert not never_looked.retrieved
    assert not never_looked.retrieved_before_acting, (
        "a subject that never ran a read verb must not pass `before-acting` just "
        "because it never wrote anything either")
