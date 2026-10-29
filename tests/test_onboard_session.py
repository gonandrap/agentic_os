"""`jarvis onboard start`: the greeting_agent session and its operator doc (docs/superpowers/specs/2026-10-28-greeting-agent-onboarding-interview.md §5)"""

from __future__ import annotations

import json
import os
import re
import shlex
import shutil
from pathlib import Path

import pytest

from jarvis import bootstrap, cli, onboard_knobs, ops, paths, seats
from jarvis.central_store import CentralStore
from jarvis.testing import make_git_project

ROOT = Path(__file__).resolve().parents[1]
ASSET = bootstrap.ASSETS / "greeting-agent" / "greeting_agent.md"


@pytest.fixture(autouse=True)
def not_a_worker(monkeypatch):
    """The suite is routinely run BY a worker, which inherits `JARVIS_WO_ID`"""
    monkeypatch.delenv("JARVIS_WO_ID", raising=False)


def test_the_fixture_really_cleared_the_worker_variable():
    assert os.environ.get("JARVIS_WO_ID") is None


@pytest.fixture()
def repo(tmp_path) -> Path:
    return make_git_project(tmp_path / "work", "shared_schedule")


@pytest.fixture()
def catalog(tmp_path) -> Path:
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"os": {}, "projects": []}))
    store = CentralStore()
    store.set_state("catalog_path", str(path))
    store.close()
    return path


@pytest.fixture()
def no_exec(monkeypatch):
    def refuse(*_a):
        raise AssertionError("the suite must never exec claude")
    monkeypatch.setattr(os, "execvp", refuse)


def _onboard_parser():
    return cli.build_parser()._subparsers._group_actions[0].choices["onboard"]


def test_every_persona_command_parses():
    parser = cli.build_parser()
    cmds = re.findall(r"`(jarvis [^`\n]+)`", ASSET.read_text(encoding="utf-8"))
    assert len(cmds) >= 4, "the persona stopped teaching its commands inline"
    for cmd in cmds:
        tokens = [re.sub(r"<[^>]*>", "x", t) or "x" for t in shlex.split(cmd)][1:]
        if all(t.isalpha() for t in tokens):
            continue
        try:
            parser.parse_args(tokens)
        except SystemExit:
            pytest.fail(f"greeting_agent teaches a command that does not parse: {cmd}")


def test_the_onboard_group_is_exactly_four_verbs():
    verbs = set(_onboard_parser()._subparsers._group_actions[0].choices)
    assert verbs == {"start", "questions", "explain", "apply"}
    args = cli.build_parser().parse_args(["onboard", "start", "/tmp/x"])
    assert (args.ob_cmd, args.path) == ("start", "/tmp/x")


def test_a_fresh_project_gets_an_empty_draft_and_the_persona(repo, catalog):
    data = ops.onboard_session(repo)

    assert data["adopted"] is False
    assert data["unanswered"] == list(onboard_knobs.AREA_KEYS)
    assert json.loads(Path(data["draft"]).read_text())["answers"] == []
    argv = data["argv"]
    assert argv[argv.index("--agent") + 1] == "greeting_agent"
    assert ["--add-dir", data["persona_root"]] == argv[1:3]
    assert ["--add-dir", str(repo.resolve())] == argv[3:5]
    assert argv[-1] == data["prompt"] and data["draft"] in data["prompt"]


def test_a_rerun_is_prefilled_from_the_live_config(repo, catalog):
    catalog.write_text(json.dumps({"os": {}, "projects": [{
        "name": "shared_schedule", "path": str(repo.resolve()), "description": "d",
        "worker": {"budget_usd": 7.5}}]}))

    data = ops.onboard_session(repo)

    draft = json.loads(Path(data["draft"]).read_text())
    assert data["adopted"] is True and "already in the catalog" in data["prompt"]
    assert draft["values"] == {"projects.shared_schedule.worker.budget_usd": 7.5}
    assert {a["area"] for a in draft["answers"]} == set(onboard_knobs.AREA_KEYS)


def test_a_non_git_path_is_refused_before_anything_is_written(tmp_path, catalog):
    plain = tmp_path / "plain"
    plain.mkdir()
    with pytest.raises(ops.OpsError, match="not a git repository"):
        ops.onboard_session(plain)
    assert not paths.onboarding_dir().exists()


def test_the_materialised_persona_is_the_packaged_asset(repo, catalog):
    root = Path(ops.onboard_session(repo)["persona_root"])
    installed = root / ".claude" / "agents" / "greeting_agent.md"

    assert installed.read_bytes() == ASSET.read_bytes()
    meta, body = seats.parse_definition(installed.read_text(encoding="utf-8"))
    assert meta["name"] == "greeting_agent" and body


def test_cwd_is_jarvis_home_and_print_prompt_launches_nothing(repo, catalog, no_exec,
                                                              capsys):
    data = ops.onboard_session(repo)
    assert data["cwd"] == str(paths.jarvis_home())
    assert not Path(data["cwd"]).resolve().is_relative_to(repo.resolve())

    shutil.rmtree(paths.onboarding_dir())
    assert cli.main(["onboard", "start", str(repo), "--print-prompt"]) == 0

    out = capsys.readouterr().out
    assert "--agent" not in out and data["prompt"] in out
    assert (Path(data["persona_root"]) / ".claude/agents/greeting_agent.md").exists()
    assert Path(data["draft"]).exists()


def test_the_operator_doc_is_reachable():
    assert (ROOT / "docs/os/operator/onboarding.md").exists()
    assert "operator/onboarding.md" in (ROOT / "docs/os/index.md").read_text()


def test_adopt_points_at_onboard():
    top = cli.build_parser()._subparsers._group_actions[0]
    adopt = next(a for a in top._choices_actions if a.dest == "adopt")
    assert "jarvis onboard" in adopt.help
