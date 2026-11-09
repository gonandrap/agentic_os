"""The draft as a page: `onboard_view`, `onboard_edit`, `onboard_drafts` (docs/superpowers/specs/2026-11-09-onboarding-from-the-dashboard.md §2)"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from jarvis import (config_version, interview, onboard_knobs, onboard_prose, ops, paths,
                    wiring)
from jarvis.catalog import parse_catalog
from jarvis.central_store import CentralStore
from jarvis.testing import make_git_project

NAME = "newproj"


@pytest.fixture(autouse=True)
def not_a_worker(monkeypatch):
    """The suite is routinely run BY a worker, which inherits `JARVIS_WO_ID`"""
    monkeypatch.delenv("JARVIS_WO_ID", raising=False)


def test_the_fixture_really_cleared_the_worker_variable():
    assert os.environ.get("JARVIS_WO_ID") is None


@pytest.fixture(autouse=True)
def no_inventory(monkeypatch):
    """`wiring.discover` execs `claude`; the view reads one fixed lever instead"""
    item = wiring.Item(kind="skill", name="s", source="user", lever=f"{wiring.LEVER_SKILL}s")
    monkeypatch.setattr(interview, "wiring_choices",
                        lambda draft, *, document: {"items": [item]})


@pytest.fixture()
def repo(tmp_path) -> Path:
    return make_git_project(tmp_path / "work", NAME)


@pytest.fixture()
def catalog(tmp_path) -> Path:
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"os": {}, "projects": []}))
    store = CentralStore()
    store.set_state("catalog_path", str(path))
    store.close()
    return path


@pytest.fixture()
def draft_file(repo, catalog) -> Path:
    interview.new_draft(repo, NAME)
    return interview.draft_file(NAME)


def _edit(verb, **fields):
    return ops.onboard_edit(NAME, verb, rev=ops.onboard_view(NAME)["rev"], **fields)


def _knob(view, path):
    return next(k for s in view["sections"] for k in s["knobs"] if k["path"] == path)


def _bool_knob(view, prefix):
    return next(k for s in view["sections"] if s["key"] not in ("identity", "wiring")
                for k in s["knobs"]
                if k["path"].startswith(prefix) and isinstance(k["default"], bool))


def test_sections_follow_the_registry_order(draft_file):
    scope = {a.key: a.scope for a in onboard_prose.AREAS}
    must = [k for k in onboard_knobs.AREA_KEYS if k in onboard_knobs.MUST_HAVE]
    rest = [k for k in onboard_knobs.AREA_KEYS if k not in must]
    expected = (["identity"] + must + [k for k in rest if scope[k] in ("project", "both")]
                + [k for k in rest if scope[k] == "os"] + ["review"])

    view = ops.onboard_view(NAME)

    assert [s["key"] for s in view["sections"]] == expected
    assert view["fleet_warning"] is None and view["problems"] == []
    lever = _knob(view, f"{wiring.LEVER_SKILL}s")
    assert lever["kind"] == "lever" and lever["value"] is True
    identity = [k["path"] for k in view["sections"][0]["knobs"]]
    assert identity[:3] == ["project.path", "project.description", "project.model"]
    assert f"projects.{NAME}.production.path" in identity


def test_value_default_and_source_from_each_origin(draft_file, catalog):
    view = ops.onboard_view(NAME)
    os_knob = _bool_knob(view, "os.")
    proj_knob = _bool_knob(view, f"projects.{NAME}.")
    other = next(k for s in view["sections"] if s["key"] not in ("identity", "wiring")
                 for k in s["knobs"] if k["path"] not in (os_knob["path"], proj_knob["path"]))
    document = json.loads(catalog.read_text())
    container, leaf = ops._document_slot(document, os_knob["path"], create=True)
    container[leaf] = not os_knob["default"]
    catalog.write_text(json.dumps(document))
    _edit("set", path=proj_knob["path"], value=json.dumps(not proj_knob["default"]))

    view = ops.onboard_view(NAME)
    blank = config_version.resolve(parse_catalog(
        {"os": {}, "projects": [{"name": NAME, "path": str(draft_file.parent)}]}))
    shipped = config_version.resolve(parse_catalog(json.loads(json.dumps(document))))
    for path, source, value in ((os_knob["path"], "catalog", shipped[os_knob["path"]]),
                                (proj_knob["path"], "draft", not blank[proj_knob["path"]]),
                                (other["path"], "default", blank[other["path"]])):
        knob = _knob(view, path)
        assert (knob["source"], knob["value"], knob["default"]) == \
            (source, value, blank[path]), path
    assert shipped[os_knob["path"]] != blank[os_knob["path"]]


def test_the_view_never_acks_the_fleet(draft_file, catalog, tmp_path):
    other = make_git_project(tmp_path / "work2", "other")
    catalog.write_text(json.dumps({"os": {}, "projects": [
        {"name": "other", "path": str(other)}]}))
    ops.onboard_view(NAME)
    view = ops.onboard_view(NAME)
    assert view["fleet_acked"] is False and "other" in view["fleet_warning"]
    knob = _bool_knob(view, "os.")
    value = json.dumps(not knob["default"])

    with pytest.raises(ops.OpsError, match="fleet setting"):
        _edit("set", path=knob["path"], value=value)
    assert _edit("fleet_ack")["fleet_acked"] is True
    assert _knob(_edit("set", path=knob["path"], value=value), knob["path"])["source"] == "draft"


def test_a_stale_rev_is_refused_and_writes_nothing(draft_file):
    before = draft_file.read_bytes()
    with pytest.raises(ops.OpsError, match="reload it"):
        ops.onboard_edit(NAME, "remaining", rev="0" * 64)
    assert draft_file.read_bytes() == before


def test_a_value_the_parser_refuses_leaves_the_draft_alone(draft_file):
    path = f"projects.{NAME}.evolution.mode"
    before = draft_file.read_bytes()
    with pytest.raises(ops.OpsError, match="must be one of"):
        _edit("set", path=path, value="nonsense")
    assert draft_file.read_bytes() == before


def test_false_on_a_boolean_knob_is_stored_as_false(draft_file):
    path = _bool_knob(ops.onboard_view(NAME), f"projects.{NAME}.")["path"]
    _edit("set", path=path, value="false")
    assert json.loads(draft_file.read_text())["values"][path] is False


def test_set_then_unset_and_confirm_keeps_values(draft_file):
    knob = _bool_knob(ops.onboard_view(NAME), f"projects.{NAME}.")
    area = onboard_knobs.area_of(knob["path"])[0]
    _edit("set", path=knob["path"], value=json.dumps(not knob["default"]))
    view = _edit("unset", path=knob["path"])
    assert knob["path"] not in json.loads(draft_file.read_text())["values"]
    assert _knob(view, knob["path"])["source"] == "default"

    _edit("set", path=knob["path"], value=json.dumps(not knob["default"]))
    kept = json.loads(draft_file.read_text())["values"]
    view = _edit("confirm", section=area)
    data = json.loads(draft_file.read_text())
    assert data["values"] == kept
    assert area in {a["area"] for a in data["answers"]}
    assert next(s for s in view["sections"] if s["key"] == area)["status"] != "untouched"


def test_a_broken_draft_renders_and_a_corrupt_file_is_listed(draft_file):
    data = json.loads(draft_file.read_text())
    data["values"] = {f"projects.{NAME}.evolution.mode": "nonsense"}
    draft_file.write_text(json.dumps(data))
    (paths.onboarding_dir() / "corrupt.json").write_text("{not json")

    assert ops.onboard_view(NAME)["problems"]
    rows = {r["name"]: r for r in ops.onboard_drafts()}
    assert "not valid JSON" in rows["corrupt"]["error"]
    assert rows[NAME]["error"] is None and rows[NAME]["total"] == 27
