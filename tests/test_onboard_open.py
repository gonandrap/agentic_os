"""Opening, resuming and mimicking an onboarding draft (docs/superpowers/specs/2026-11-09-onboarding-from-the-dashboard.md §1)"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from jarvis import cli, config_version, interview, onboard_knobs, onboard_prose, ops
from jarvis.catalog import parse_catalog
from jarvis.central_store import CentralStore
from jarvis.testing import make_git_project


@pytest.fixture(autouse=True)
def not_a_worker(monkeypatch):
    """The suite is routinely run BY a worker, which inherits `JARVIS_WO_ID`"""
    monkeypatch.delenv("JARVIS_WO_ID", raising=False)


def test_the_fixture_really_cleared_the_worker_variable():
    assert os.environ.get("JARVIS_WO_ID") is None


@pytest.fixture()
def view(monkeypatch):
    """§2 owns `onboard_view`; stub it so these tests read the draft FILE"""
    monkeypatch.setattr(ops, "onboard_view",
                        lambda name, catalog_path=None: {"project": {"name": name}},
                        raising=False)


@pytest.fixture()
def repo(tmp_path) -> Path:
    return make_git_project(tmp_path / "work", "newproj")


def _source(tmp_path) -> dict:
    return {"os": {"neo": {"enabled": False}}, "projects": [{
        "name": "src", "path": str(tmp_path), "description": "the source",
        "model": "opus",
        "validation": {"enabled": True},
        "production": {"path": str(tmp_path / "prod")},
        "bugs": {"repo": "someone/src"},
        "worker": {"launcher": {"command": [str(tmp_path / "wrap.sh")]}}}]}


@pytest.fixture()
def catalog(tmp_path) -> Path:
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(_source(tmp_path)))
    store = CentralStore()
    store.set_state("catalog_path", str(path))
    store.close()
    return path


def _draft(name: str = "newproj") -> dict:
    return json.loads(interview.draft_file(name).read_text())


def test_like_copies_settings_never_identity_nor_os(tmp_path, repo):
    document = _source(tmp_path)
    source_resolved = config_version.resolve(parse_catalog(json.loads(json.dumps(document))))
    written = ops._written_paths(document, source_resolved)
    tails = ("validation.enabled", "production.path", "path", "bugs.repo", "worker.launcher")
    for tail in tails:  # control: the source DID set each one
        assert any(p.startswith(f"projects.src.{tail}") for p in written), tail
    assert any(p.startswith("os.") for p in written)

    draft = interview.like_draft(repo, "newproj", "src", document=document,
                                 areas=onboard_prose.AREAS)

    values = _draft()["values"]
    assert values["projects.newproj.validation.enabled"] is True
    for tail in (*tails[1:], "description"):
        assert not any(p.startswith(f"projects.newproj.{tail}") for p in values), tail
    assert not any(p.startswith("os.") for p in values)
    assert draft.project.model == "opus" and draft.project.description == ""
    assert {"area": "like", "mode": "src"} in draft.answers
    answered = {a["area"] for a in draft.answers}
    assert not answered & onboard_knobs.MUST_HAVE
    assert answered - {"like"}  # control: non-must-have areas ARE answered


def test_like_never_copies_a_default_the_source_never_wrote(tmp_path, repo):
    document = _source(tmp_path)
    resolved = config_version.resolve(parse_catalog(json.loads(json.dumps(document))))
    assert "projects.src.worker.budget_usd" in resolved  # control: it resolves

    interview.like_draft(repo, "newproj", "src", document=document, areas=())

    assert "projects.newproj.worker.budget_usd" not in _draft()["values"]


def test_like_an_unknown_source_names_the_projects(tmp_path, repo, catalog):
    with pytest.raises(interview.InterviewError, match="src"):
        interview.like_draft(repo, "newproj", "nope", document=_source(tmp_path), areas=())

    ops.onboard_session(repo, like="src")
    assert _draft()["values"]["projects.newproj.validation.enabled"] is True


def test_open_resumes_fresh_rebuilds_like_refuses(repo, catalog, view):
    ops.onboard_open(repo)
    edited = interview.draft_file("newproj")
    data = _draft()
    data["reason"] = "half way"
    edited.write_text(json.dumps(data))

    ops.onboard_open(repo)
    assert _draft()["reason"] == "half way"
    with pytest.raises(ops.OpsError, match="already exists — continue it"):
        ops.onboard_open(repo, like="src")
    ops.onboard_open(repo, fresh=True)
    assert _draft()["reason"] == ""


def test_like_on_an_adopted_project_is_refused(tmp_path, catalog, view):
    src = make_git_project(tmp_path / "w2", "src")
    with pytest.raises(ops.OpsError, match="--like is only for a new project"):
        ops.onboard_open(src, like="src")


def test_parser_takes_like_and_fresh_and_keeps_four_verbs():
    args = cli.build_parser().parse_args(["onboard", "start", "/x", "--like", "a", "--fresh"])
    assert (args.like, args.fresh) == ("a", True)
    ob = cli.build_parser()._subparsers._group_actions[0].choices["onboard"]
    assert set(ob._subparsers._group_actions[0].choices) == {
        "start", "questions", "explain", "apply"}


def test_session_keeps_an_edit_unless_fresh(repo, catalog):
    ops.onboard_session(repo)
    file = interview.draft_file("newproj")
    data = _draft()
    data["reason"] = "mine"
    file.write_text(json.dumps(data))

    ops.onboard_session(repo)
    assert _draft()["reason"] == "mine"
    ops.onboard_session(repo, fresh=True)
    assert _draft()["reason"] == ""


def _adopted_with_edit(tmp_path, catalog) -> Path:
    src = make_git_project(tmp_path / "w3", "src")
    document = _source(tmp_path)
    document["projects"][0]["path"] = str(src.resolve())
    catalog.write_text(json.dumps(document))
    ops.onboard_session(src)
    file = interview.draft_file("src")
    data = json.loads(file.read_text())
    data["reason"] = "stale"
    file.write_text(json.dumps(data))
    return src


def test_a_draft_older_than_the_head_version_is_rebuilt(tmp_path, catalog, view):
    src = _adopted_with_edit(tmp_path, catalog)
    file = interview.draft_file("src")
    store = CentralStore()
    past = store.add_config_version({"moved": True}, {}, actor="test")["ts"] - 3600
    store.close()
    os.utime(file, (past, past))

    out = ops.onboard_open(src)

    assert "rebuilt" in out and "revert" in out["rebuilt"]
    assert json.loads(file.read_text())["reason"] == ""


def test_a_draft_newer_than_the_head_version_is_resumed(tmp_path, catalog, view):
    store = CentralStore()
    future = store.add_config_version({"moved": True}, {}, actor="test")["ts"] + 3600
    store.close()
    src = _adopted_with_edit(tmp_path, catalog)
    os.utime(interview.draft_file("src"), (future, future))

    out = ops.onboard_open(src)

    assert "rebuilt" not in out
    assert json.loads(interview.draft_file("src").read_text())["reason"] == "stale"
