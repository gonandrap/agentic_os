"""The onboarding registry: every resolved config path lands in a behaviour area (docs/superpowers/specs/2026-10-28-greeting-agent-onboarding-interview.md §1)"""

from __future__ import annotations

import dataclasses
from dataclasses import fields

import pytest

from jarvis import catalog, config_version as cv, onboard_knobs as knobs

DOCUMENT = {
    "os": {},
    "projects": [{"name": "p", "path": "/tmp", "description": "one"}],
}


@pytest.fixture()
def resolved() -> dict:
    return cv.resolve(catalog.parse_catalog(DOCUMENT))


def _tails(resolved: dict) -> set[str]:
    return {hit[1] for hit in (knobs.area_of(p) for p in resolved) if hit}


def test_the_fixture_is_pinned_to_the_real_schema(resolved):
    """An empty `coverage()` proves nothing unless the resolved map is the whole schema (§1)"""
    assert len(resolved) == 386
    assert sum(1 for p in resolved if p.startswith("os.")) == 208
    assert sum(1 for p in resolved if p.startswith("projects.")) == 178


def test_every_resolved_path_is_classified(resolved):
    assert knobs.coverage(resolved) == []


def test_the_classification_is_not_one_bucket(resolved):
    assert len(knobs.AREA_KEYS) >= 25

    claimed: dict[str, set[str]] = {key: set() for key in knobs.AREA_KEYS}
    for path in resolved:
        hit = knobs.area_of(path)
        assert hit is not None
        claimed[hit[0]].add(hit[1])

    assert all(claimed[key] for key in knobs.AREA_KEYS), "a decorative area"
    assert max(len(t) for t in claimed.values()) <= len(_tails(resolved)) / 4


def test_asked_says_yes_to_something(resolved):
    asked = {hit[1] for p in resolved if knobs.asked(p) and (hit := knobs.area_of(p))}
    assert 40 <= len(asked) <= 80
    assert knobs.asked("projects.p.validation.enabled")
    assert knobs.asked("os.neo.enabled")
    assert knobs.asked("projects.p.worker.budget_usd")


def test_collections_and_unaskable_tails_are_never_asked(resolved):
    for path, value in resolved.items():
        if isinstance(value, (dict, list)):
            assert not knobs.asked(path), path

    for tail in knobs.UNASKABLE:
        scoped = [f"os.{tail}", f"projects.p.{tail}"]
        assert not any(knobs.asked(p) for p in scoped), tail
        assert any(knobs.area_of(p) for p in scoped), tail


def test_the_rename_table_lands_both_spellings_in_one_area():
    """`OsConfig.default_model` flattens to `os.defaults.model`; the project spells it `model`"""
    assert knobs.area_of("os.defaults.model")[0] == knobs.area_of("projects.p.model")[0]
    assert knobs.area_of("os.default_cache_ttl")[0] == \
        knobs.area_of("projects.p.worker.cache_ttl")[0]


def test_the_area_contract():
    names = tuple(f.name for f in fields(knobs.Area))
    assert names == ("key", "title", "scope", "summary", "on_costs", "off_costs",
                     "refs", "presets")

    area = knobs.Area(key="k", title="t", scope="os", summary="s", on_costs="on",
                      off_costs="off", refs=(("spec.md", "1"),), presets={})
    with pytest.raises(dataclasses.FrozenInstanceError):
        area.title = "other"  # type: ignore[misc]

    assert set(knobs.AREA_SCOPE) == set(knobs.AREA_KEYS)
    assert set(knobs.AREA_SCOPE.values()) <= {"os", "project", "both"}


def test_must_haves_lead_the_interview():
    assert knobs.MUST_HAVE
    assert knobs.MUST_HAVE < set(knobs.AREA_KEYS)
    lead = knobs.AREA_KEYS[:len(knobs.MUST_HAVE)]
    assert set(lead) == set(knobs.MUST_HAVE)
