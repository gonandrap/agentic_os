"""`ops.onboard_apply`: ONE config version, then the bootstrap, then the restart (docs/superpowers/specs/2026-10-28-greeting-agent-onboarding-interview.md §3)"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from jarvis import cli, config_version as cv, gate_rules, ops, spec_index
from jarvis.central_store import CentralStore
from jarvis.testing import make_git_project

SPEC = (Path(__file__).resolve().parents[1]
        / "docs/superpowers/specs/2026-10-28-greeting-agent-onboarding-interview.md")


def spec_literal() -> dict:
    """§0.3's own fenced JSON: the contract §2 and §3 share without speaking"""
    text = spec_index.section(SPEC.read_text(encoding="utf-8"), "0.3")
    assert text is not None
    return json.loads(text.split("```json", 1)[1].split("```", 1)[0])


@pytest.fixture(autouse=True)
def not_a_worker(monkeypatch):
    """The suite is routinely run BY a worker, which inherits `JARVIS_WO_ID`"""
    monkeypatch.delenv("JARVIS_WO_ID", raising=False)


@pytest.fixture()
def repo(tmp_path) -> Path:
    """No README, so the bootstrap's stub shows what description was committed"""
    return make_git_project(tmp_path / "work", "shared_schedule", readme=None)


@pytest.fixture()
def catalog(tmp_path) -> Path:
    """A registered catalog with no projects, the way `jarvis start` leaves one"""
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"os": {"defaults": {"model": "opus"}}, "projects": []}))
    store = CentralStore()
    store.set_state("catalog_path", str(path))
    store.close()
    return path


def draft_file(tmp_path: Path, path: Path, name: str = "draft.json", **over) -> Path:
    """§0.3's literal, written with a plain `json.dump` and NOT through `save`"""
    data = spec_literal()
    data["project"] = {**data["project"], "path": str(path)}
    data.update(over)
    file = tmp_path / name
    file.write_text(json.dumps(data))
    return file


def rows() -> list[dict]:
    store = CentralStore()
    try:
        return store.config_versions()
    finally:
        store.close()


def head() -> dict | None:
    store = CentralStore()
    try:
        return store.head_config_version()
    finally:
        store.close()


def entry_of(catalog: Path, name: str = "shared_schedule") -> dict:
    document = json.loads(catalog.read_text())
    return next(p for p in document["projects"] if p["name"] == name)


def test_the_fixture_really_cleared_the_worker_variable():
    assert os.environ.get("JARVIS_WO_ID") is None


# --- the write, and the four refusals

def test_a_fresh_project_is_configured_and_bootstrapped(catalog, repo, tmp_path):
    res = ops.onboard_apply(str(draft_file(tmp_path, repo)))

    assert entry_of(catalog)["path"] == str(repo)
    assert (repo / "OPERATION.md").exists()
    assert (repo / ".claude" / "settings.json").exists()
    assert any(c["path"] == "projects.shared_schedule.validation.enabled"
               for c in res["changes"])


def test_a_path_that_is_not_a_git_repository_is_refused(catalog, tmp_path):
    plain = tmp_path / "plain"
    plain.mkdir()

    with pytest.raises(ops.OpsError, match="git init"):
        ops.onboard_apply(str(draft_file(tmp_path, plain)))


def test_a_safety_move_without_a_reason_is_refused(catalog, repo, tmp_path):
    with pytest.raises(ops.OpsError, match="reason"):
        ops.onboard_apply(str(draft_file(tmp_path, repo, reason="")))
    assert head() is None


def test_a_worker_session_is_refused(catalog, repo, tmp_path, monkeypatch):
    monkeypatch.setenv("JARVIS_WO_ID", "wo-deadbeef")

    with pytest.raises(ops.OpsError, match="worker"):
        ops.onboard_apply(str(draft_file(tmp_path, repo)))


def test_an_unknown_draft_schema_is_refused(catalog, repo, tmp_path):
    with pytest.raises(ops.OpsError, match="schema"):
        ops.onboard_apply(str(draft_file(tmp_path, repo, schema=2)))


# --- one row, the spec read back, and the os.db mirror

def test_one_version_row_and_it_is_the_head(catalog, repo, tmp_path):
    before = len(rows())

    res = ops.onboard_apply(str(draft_file(tmp_path, repo)))

    assert len(rows()) == before + 1
    assert head()["id"] == res["version"]["id"]


def test_the_bootstrapped_spec_is_read_back_out_of_the_committed_catalog(
        catalog, repo, tmp_path):
    draft = draft_file(tmp_path, repo, project={
        "name": "shared_schedule", "path": str(repo),
        "description": "the shared calendar", "model": "haiku"})

    ops.onboard_apply(str(draft))

    entry = entry_of(catalog)
    assert entry["description"] == "the shared calendar" and entry["model"] == "haiku"
    assert "the shared calendar" in (repo / "README.md").read_text()


def test_upsert_project_mirrors_start_os(catalog, repo, tmp_path):
    ops.onboard_apply(str(draft_file(tmp_path, repo)))

    store = CentralStore()
    try:
        row = store.get_project("shared_schedule")
    finally:
        store.close()
    assert json.loads(row["catalog_json"])["name"] == "shared_schedule"


def test_the_version_is_committed_before_the_bootstrap(catalog, repo, tmp_path,
                                                       monkeypatch):
    from jarvis import bootstrap

    def explode(*_a, **_k):
        raise RuntimeError("bootstrap blew up")

    monkeypatch.setattr(bootstrap, "bootstrap_project", explode)

    with pytest.raises(RuntimeError, match="bootstrap blew up"):
        ops.onboard_apply(str(draft_file(tmp_path, repo)))

    document = json.loads(catalog.read_text())
    assert head()["id"] == cv.version_id(document)
    assert entry_of(catalog)["path"] == str(repo)


# --- the dry run, the restart, and the second apply

def test_the_dry_run_tells_the_truth(catalog, repo, tmp_path):
    draft = draft_file(tmp_path, repo)
    text, before = catalog.read_text(), len(rows())

    dry = ops.onboard_apply(str(draft), dry_run=True)

    assert catalog.read_text() == text and len(rows()) == before
    assert dry["version"] is None and dry["bootstrap"] is None

    real = ops.onboard_apply(str(draft))
    assert dry["changes"] == real["changes"]
    assert dry["restart_needed"] == real["restart_needed"]


def test_restart_needed_for_a_new_project_and_not_for_a_hot_change(catalog, repo,
                                                                  tmp_path):
    first = ops.onboard_apply(str(draft_file(tmp_path, repo)))
    assert first["restart_needed"] and "shared_schedule" in first["restart_why"]

    hot = draft_file(tmp_path, repo, name="hot.json", reason="too noisy after all",
                     values={"projects.shared_schedule.validation.enabled": False})
    res = ops.onboard_apply(str(hot))

    assert res["changes"] and res["restart_needed"] is False
    assert res["apply"] == {"projects.shared_schedule.validation.enabled": "hot"}


def test_the_cli_prints_the_restart_command(catalog, repo, tmp_path, capsys):
    rc = cli.main(["onboard", "apply", "--from", str(draft_file(tmp_path, repo))])

    out = capsys.readouterr().out
    assert rc == 0
    assert f"jarvis start --catalog {catalog}" in out


def test_re_applying_an_unchanged_draft_writes_no_second_row(catalog, repo, tmp_path):
    draft = draft_file(tmp_path, repo)
    ops.onboard_apply(str(draft))
    before = len(rows())

    res = ops.onboard_apply(str(draft))

    assert len(rows()) == before and res["changed"] is False


# --- the gate

def test_onboard_apply_gates_as_a_config_write():
    ruleset = gate_rules.RuleSet.from_seeds()

    match = ruleset.decide("jarvis onboard apply --from /tmp/draft.json",
                           gate_rules.KIND_NAMES).match
    assert match is not None and match.kind == "config_write"
    assert ruleset.check_canaries() == []
