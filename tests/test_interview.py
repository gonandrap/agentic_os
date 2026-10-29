"""The onboarding interview (docs/superpowers/specs/2026-10-28-greeting-agent-onboarding-interview.md §2)"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from jarvis import config_version as cv
from jarvis import interview, onboard_draft, ops, paths, spec_index
from jarvis.catalog import CatalogError, parse_catalog
from jarvis.onboard_knobs import Area

SPEC = (Path(__file__).resolve().parents[1]
        / "docs/superpowers/specs/2026-10-28-greeting-agent-onboarding-interview.md")


def _area(key: str, scope: str, presets: dict) -> Area:
    return Area(key=key, title=key, scope=scope, summary="s", on_costs="on", off_costs="off",
                refs=(), presets=presets)


# The test's own presets, not §4's corpus; `off` turns the supervisor sweep off on purpose
AREAS = (
    _area("validation", "both", {"balanced": {"validation.enabled": True},
                                 "off": {"validation.enabled": False}}),
    _area("neo", "os", {"balanced": {"neo.enabled": True},
                        "off": {"neo.enabled": False}}),
    _area("supervisor", "both", {"balanced": {"supervisor.enabled": True},
                                 "off": {"supervisor.enabled": False,
                                         "supervisor.health_enabled": False}}),
    _area("budgets", "both", {"cautious": {"worker.budget_usd": 1.0}}),
)


@pytest.fixture(autouse=True)
def not_a_worker(monkeypatch, tmp_path):
    """The suite is routinely run BY a worker, which inherits `JARVIS_WO_ID`"""
    monkeypatch.delenv("JARVIS_WO_ID", raising=False)
    monkeypatch.setenv("JARVIS_HOME", str(tmp_path / "home"))


def test_the_fixture_really_cleared_the_worker_variable():
    assert os.environ.get("JARVIS_WO_ID") is None


@pytest.fixture()
def project(tmp_path) -> Path:
    path = tmp_path / "proj"
    path.mkdir()
    (path / "README.md").write_text("x")
    return path


def _fleet(tmp_path) -> dict:
    return {"os": {}, "projects": [{"name": "other", "path": str(tmp_path)}]}


def test_accept_defaults_writes_no_values_and_leaves_the_document_silent(project):
    document = {"os": {}, "projects": []}
    draft = interview.new_draft(project, "proj")

    interview.accept_defaults(draft, "validation", document=document)
    interview.accept_remaining(draft, document=document)

    assert draft.values == {}
    entry = onboard_draft.candidate_document(draft, document)["projects"][0]
    assert set(entry) == {"name", "path"}


def test_the_fleet_warning_is_enforced_not_displayed(project, tmp_path):
    document = _fleet(tmp_path)
    draft = interview.new_draft(project, "proj")

    with pytest.raises(interview.InterviewError, match="other"):
        interview.answer(draft, "neo", "preset:balanced", areas=AREAS, document=document)
    assert not [p for p in draft.values if p.startswith("os.")]
    assert not [p for p in onboard_draft.load(interview.draft_file("proj")).values
                if p.startswith("os.")]

    assert "other" in interview.fleet_warning(draft, document)
    interview.answer(draft, "neo", "preset:balanced", areas=AREAS, document=document)
    assert draft.values == {"os.neo.enabled": True}


def test_a_both_area_writes_the_project_spelling(project, tmp_path):
    draft = interview.new_draft(project, "proj")
    interview.answer(draft, "validation", "preset:off", areas=AREAS,
                     document=_fleet(tmp_path))
    assert draft.values == {"projects.proj.validation.enabled": False}


def test_the_os_projects_sweep_is_never_offered(project, monkeypatch):
    monkeypatch.setattr("jarvis.schedule.os_project", lambda pairs, **kw: "proj")
    document = {"os": {}, "projects": []}
    draft = interview.new_draft(project, "proj")

    asked = interview.questions(draft, AREAS, document=document)
    offered = [values for q in asked for values in q.postures.values()]

    assert [q.area for q in asked] == ["validation", "neo", "supervisor", "budgets"]
    assert set(next(q for q in asked if q.area == "supervisor").postures) == {"balanced"}
    for values in offered:
        for path, value in values.items():
            if path.endswith(("supervisor.enabled", "supervisor.health_enabled")):
                assert value is True
    with pytest.raises(interview.InterviewError, match="sweep"):
        interview.answer(draft, "supervisor", "preset:off", areas=AREAS, document=document)


def test_summary_equals_the_real_resolved_diff(project, tmp_path):
    document = _fleet(tmp_path)
    draft = interview.new_draft(project, "proj")
    interview.answer(draft, "validation", "preset:off", areas=AREAS, document=document)
    interview.answer(draft, "budgets", "preset:cautious", areas=AREAS, document=document)

    rows = interview.summary(draft, document)

    before = cv.resolve(parse_catalog(json.loads(json.dumps(document))))
    after = cv.resolve(parse_catalog(json.loads(json.dumps(
        onboard_draft.candidate_document(draft, document)))))
    expected = cv.diff(before, after)
    assert len(expected) > 1
    assert [{k: r[k] for k in ("path", "kind", "old", "new")} for r in rows] == expected
    for row in rows:
        assert row["apply"] == ops.apply_class(row["path"])
        assert row["safety"] == ops.safety_key(row["path"])


def test_the_parser_is_the_type_system(project):
    document = {"os": {}, "projects": []}
    draft = interview.new_draft(project, "proj")
    bad = {"os": {}, "projects": [{"name": "proj", "path": str(project.resolve()),
                                   "worker": {"permission_mode": "nonsense"}}]}
    with pytest.raises(CatalogError) as parsed:
        parse_catalog(bad)

    with pytest.raises(interview.InterviewError) as refused:
        interview.set_value(draft, "projects.proj.worker.permission_mode", "nonsense",
                            document=document)

    assert str(parsed.value) in str(refused.value)
    assert draft.values == {}


def test_a_rerun_prefills_the_matching_posture_and_custom(project):
    document = {"os": {}, "projects": [{
        "name": "proj", "path": str(project.resolve()), "description": "d",
        "validation": {"enabled": True}, "worker": {"budget_usd": 7.5}}]}
    current = ops._in_scope(cv.resolve(parse_catalog(json.loads(json.dumps(document)))),
                            "proj")

    draft = interview.new_draft(project, "proj", current, areas=AREAS, document=document)

    modes = {a["area"]: a["mode"] for a in draft.answers}
    assert modes["validation"] == "preset:balanced"
    assert modes["budgets"] == "custom"
    assert draft.values == {"projects.proj.validation.enabled": True,
                            "projects.proj.worker.budget_usd": 7.5}
    assert draft.project.description == "d"


def test_the_repository_stays_clean(project):
    listing = sorted(p.name for p in project.rglob("*"))
    draft = interview.new_draft(project, "proj")
    interview.answer(draft, "validation", "preset:balanced", areas=AREAS,
                     document={"os": {}, "projects": []})

    assert sorted(p.name for p in project.rglob("*")) == listing
    file = interview.draft_file("proj")
    assert file.is_relative_to(paths.jarvis_home())
    assert [p.name for p in file.parent.iterdir()] == [file.name]


def _spec_draft() -> dict:
    text = spec_index.section(SPEC.read_text(encoding="utf-8"), "0.3")
    assert text is not None
    return json.loads(text.split("```json", 1)[1].split("```", 1)[0])


def test_the_spec_draft_round_trips_and_schema_2_is_refused(tmp_path):
    literal = _spec_draft()
    on_disk = tmp_path / "draft.json"
    on_disk.write_text(json.dumps(literal))

    interview.save(onboard_draft.load(on_disk))
    back = json.loads(interview.draft_file(literal["project"]["name"]).read_text())

    assert back == literal
    assert set(back) == {"schema", "project", "values", "answers", "reason"}
    assert set(back["project"]) == {"name", "path", "description", "model"}
    on_disk.write_text(json.dumps({**literal, "schema": 2}))
    with pytest.raises(onboard_draft.DraftError, match="schema"):
        onboard_draft.load(on_disk)


def test_wiring_starts_from_the_runtime_config_and_levers_compose(project, monkeypatch):
    from jarvis.catalog import WiringConfig

    scopes = []
    monkeypatch.setattr(ops, "wiring_config", lambda project=None, catalog_path=None:
                        scopes.append(project) or WiringConfig(disabled_skills=("live",)))
    monkeypatch.setattr(ops, "wiring_show", lambda project=None, **kw:
                        {"project": project, "items": ["x"]})
    document = {"os": {}, "projects": []}
    draft = interview.new_draft(project, "proj")

    interview.wire(draft, "skill:a", False, document=document)
    interview.wire(draft, "skill:b", False, document=document)

    assert draft.values == {"projects.proj.wiring.disabled_skills": ["a", "b", "live"]}
    # Not in the catalog yet, so the fleet base, never `project_spec`'s refusal
    assert scopes == [None, None]
    assert interview.wiring_choices(draft, document=document) == {"project": None,
                                                                  "items": ["x"]}
