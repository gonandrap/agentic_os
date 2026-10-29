"""The onboarding draft file and the document it becomes (docs/superpowers/specs/2026-10-28-greeting-agent-onboarding-interview.md §3)"""

from __future__ import annotations

import copy
import json
import os
from pathlib import Path

import pytest

from jarvis import config_version as cv, onboard_draft, spec_index
from jarvis.catalog import parse_catalog

SPEC = (Path(__file__).resolve().parents[1]
        / "docs/superpowers/specs/2026-10-28-greeting-agent-onboarding-interview.md")


def spec_draft() -> dict:
    """§0.3's own fenced JSON: the contract §2 and §3 share without speaking"""
    text = spec_index.section(SPEC.read_text(encoding="utf-8"), "0.3")
    assert text is not None
    return json.loads(text.split("```json", 1)[1].split("```", 1)[0])


@pytest.fixture(autouse=True)
def not_a_worker(monkeypatch):
    """The suite is routinely run BY a worker, which inherits `JARVIS_WO_ID`"""
    monkeypatch.delenv("JARVIS_WO_ID", raising=False)


def test_the_fixture_really_cleared_the_worker_variable():
    assert os.environ.get("JARVIS_WO_ID") is None


def test_a_draft_round_trips_through_save_and_load(tmp_path):
    literal = spec_draft()
    draft = onboard_draft.Draft(
        project=onboard_draft.DraftProject(**literal["project"]),
        reason=literal["reason"], values=literal["values"],
        answers=literal["answers"])

    path = onboard_draft.save(draft, tmp_path / "sub" / "draft.json")
    back = onboard_draft.load(path)

    assert onboard_draft.as_dict(back) == literal


def test_an_unknown_schema_is_refused(tmp_path):
    path = tmp_path / "draft.json"
    path.write_text(json.dumps({**spec_draft(), "schema": 2}))

    with pytest.raises(onboard_draft.DraftError, match="schema"):
        onboard_draft.load(path)


def test_the_candidate_places_both_scopes_and_mutates_nothing(tmp_path):
    draft = onboard_draft.Draft(
        project=onboard_draft.DraftProject(name="shared_schedule", path=str(tmp_path)),
        values={"os.neo.enabled": False,
                "projects.shared_schedule.validation.enabled": True})
    document = {"os": {"defaults": {"model": "opus"}}, "projects": []}
    untouched = copy.deepcopy(document)

    candidate = onboard_draft.candidate_document(draft, document)

    assert document == untouched
    resolved = cv.resolve(parse_catalog(candidate))
    assert resolved["os.neo.enabled"] is False
    assert resolved["projects.shared_schedule.validation.enabled"] is True
    # An accepted default leaves the document silent, so no `model` key was written
    assert set(candidate["projects"][0]) == {"name", "path", "validation"}


def test_validate_returns_the_parsers_own_words_and_nothing_for_a_good_draft(tmp_path):
    document = {"os": {}, "projects": []}
    good = onboard_draft.Draft(
        project=onboard_draft.DraftProject(name="shared_schedule", path=str(tmp_path)),
        values={"os.neo.enabled": False})
    bad = onboard_draft.Draft(
        project=onboard_draft.DraftProject(name="shared_schedule", path=str(tmp_path)),
        values={"os.defaults.permission_mode": "nonsense"})

    assert onboard_draft.validate(good, document) == []
    problems = onboard_draft.validate(bad, document)
    assert len(problems) == 1 and "permission_mode" in problems[0]
