"""The onboarding corpus and `jarvis onboard questions | explain` (docs/superpowers/specs/2026-10-28-greeting-agent-onboarding-interview.md §4)"""

from __future__ import annotations

import json
import os
import re
import shlex
from pathlib import Path

import pytest

from jarvis import catalog, cli, config_version, onboard_knobs, onboard_prose, ops, spec_index

REPO = Path(__file__).resolve().parents[1]
AREAS = {a.key: a for a in onboard_prose.AREAS}


@pytest.fixture(autouse=True)
def not_a_worker(monkeypatch):
    """The suite is routinely run BY a worker, which inherits `JARVIS_WO_ID`"""
    monkeypatch.delenv("JARVIS_WO_ID", raising=False)
    assert os.environ.get("JARVIS_WO_ID") is None


def _defaults() -> dict:
    return config_version.resolve(catalog.parse_catalog(
        {"os": {}, "projects": [{"name": "_", "path": "/", "description": ""}]}))


def _asked_tails() -> dict[str, str]:
    """Canonical asked tail to a `_`-project path it resolves at"""
    out: dict[str, str] = {}
    for path in _defaults():
        if onboard_knobs.asked(path):
            out.setdefault(onboard_knobs.area_of(path)[1], path)
    return out


def _spell(area: onboard_knobs.Area, tail: str, project: str = "_") -> str:
    return f"os.{tail}" if area.scope == "os" else f"projects.{project}.{tail}"


def test_every_ref_resolves_and_parses_as_the_command_the_persona_runs():
    parser = cli.build_parser()
    for area in onboard_prose.AREAS:
        assert area.refs, area.key
        for path, section in area.refs:
            file = REPO / path
            assert file.is_file(), (area.key, path)
            assert spec_index.section(file.read_text(), section), (area.key, path, section)
            args = parser.parse_args(["spec", "section", path, section])
            assert (args.path, args.which) == (path, section)


def test_one_area_per_key_and_every_asked_tail_has_a_line_and_an_example():
    assert [a.key for a in onboard_prose.AREAS] == list(onboard_knobs.AREA_KEYS)
    asked = set(_asked_tails())
    assert asked <= set(onboard_prose.LINES), asked - set(onboard_prose.LINES)
    assert asked <= set(onboard_prose.EXAMPLES), asked - set(onboard_prose.EXAMPLES)
    for area in onboard_prose.AREAS:
        assert area.scope == onboard_knobs.AREA_SCOPE[area.key]


def test_prose_has_a_floor():
    for area in onboard_prose.AREAS:
        texts = (area.summary, area.on_costs, area.off_costs)
        assert len(set(texts)) == 3, area.key
        for text in texts:
            assert len(text) >= 80, (area.key, text)
            assert text.endswith("."), (area.key, text)


def test_a_preset_only_moves_knobs_its_own_area_owns():
    for area in onboard_prose.AREAS:
        assert set(area.presets) <= set(onboard_prose.POSTURES), area.key
        for posture, preset in area.presets.items():
            assert preset, (area.key, posture)
            for tail in preset:
                path = _spell(area, tail)
                assert onboard_knobs.area_of(path) == (area.key, tail), (area.key, tail)
                assert onboard_knobs.asked(path), (area.key, tail)


def test_every_enable_knob_is_an_asked_knob_of_its_area():
    for key, tail in onboard_prose.ENABLE.items():
        assert tail in _asked_tails(), key
        assert onboard_knobs.area_of(_asked_tails()[tail])[0] == key


def test_every_preset_survives_the_catalog_parser():
    for area in onboard_prose.AREAS:
        for posture, preset in area.presets.items():
            document = {"os": {}, "projects": [{"name": "p", "path": "/tmp",
                                                "description": ""}]}
            for tail, value in preset.items():
                slot, key = ops._document_slot(document, _spell(area, tail, "p"),
                                               create=True)
                slot[key] = value
            resolved = config_version.resolve(catalog.parse_catalog(document))
            for tail, value in preset.items():
                assert resolved[_spell(area, tail, "p")] == value, (area.key, posture, tail)


AREA_KEYS = {"kind", "area", "title", "scope", "must_have", "summary", "on_costs",
             "off_costs", "enable", "postures", "grounding"}
PATH_KEYS = {"kind", "path", "tail", "area", "line", "example", "current", "default",
             "apply", "apply_note", "safety", "command"}


def test_explain_answers_every_area_with_a_pinned_shape():
    for key in onboard_knobs.AREA_KEYS:
        out = ops.onboard_explain(key)
        assert set(out) == AREA_KEYS, key
        assert all(g["excerpt"] for g in out["grounding"]), key
        for rows in out["postures"].values():
            assert all(set(r) == {"path", "value", "line"} and r["line"] for r in rows)


def test_explain_answers_every_asked_tail_with_apply_and_safety_from_ops():
    parser = cli.build_parser()
    for tail in _asked_tails():
        # The `model` tail shares its spelling with the `model` area, which wins; a full path reaches the knob
        target = (f"projects.{ops.ONBOARD_PLACEHOLDER}.{tail}"
                  if tail in onboard_knobs.AREA_KEYS else tail)
        out = ops.onboard_explain(target)
        assert set(out) == PATH_KEYS, tail
        assert out["tail"] == tail and out["line"] and out["example"]
        assert out["apply"] == ops.apply_class(out["path"])
        assert out["apply_note"] == ops.APPLY_NOTES[out["apply"]]
        assert out["safety"] == ops.safety_key(out["path"])
        argv = shlex.split(out["command"])
        assert argv[:3] == ["jarvis", "config", "set"]
        args = parser.parse_args(argv[1:])
        if out["path"].startswith("projects."):
            assert args.project == ops.ONBOARD_PLACEHOLDER and args.path == tail
        else:
            assert args.project is None and args.path == out["path"]
        assert ops.parse_config_value(args.value) == out["current"]


def test_explain_refuses_an_unknown_target_listing_the_areas():
    with pytest.raises(ops.OpsError) as e:
        ops.onboard_explain("nonsense.knob")
    assert "validation" in str(e.value) and "steward" in str(e.value)


def test_explain_reads_current_values_from_a_named_project(tmp_path):
    file = tmp_path / "catalog.json"
    file.write_text(json.dumps({"os": {}, "projects": [
        {"name": "shop", "path": str(tmp_path), "description": "",
         "validation": {"enabled": True}}]}))
    out = ops.onboard_explain("validation.enabled", project="shop", catalog_path=str(file))
    assert out["path"] == "projects.shop.validation.enabled"
    assert (out["current"], out["default"]) == (True, False)
    args = cli.build_parser().parse_args(shlex.split(out["command"])[1:])
    assert args.project == "shop"


def test_numbers_in_an_example_come_from_the_shipped_default():
    for tail, path in _asked_tails().items():
        default = _defaults()[path]
        if isinstance(default, bool) or not isinstance(default, (int, float)):
            continue
        numbers = [float(n) for n in re.findall(r"\d+(?:\.\d+)?", onboard_prose.EXAMPLES[tail])]
        if numbers:
            assert float(default) in numbers, (tail, default, numbers)


def test_the_wiring_wording_cannot_fork():
    from jarvis.ui.app import WIRING_EVIDENCE, WIRING_GROUPS

    text = " ".join((AREAS["wiring"].summary, AREAS["wiring"].on_costs))
    for key, _label, blurb in WIRING_GROUPS:
        if key in ("mcp", "plugin"):
            assert blurb in text
    assert WIRING_EVIDENCE["connectors"] in text


def test_questions_is_the_whole_catalogue_and_the_cli_prints_it(capsys):
    out = ops.onboard_questions()
    assert [a["area"] for a in out["areas"]] == list(onboard_knobs.AREA_KEYS)
    asked = {k["tail"] for a in out["areas"] for k in a["knobs"]["asked"]}
    assert asked == set(_asked_tails())
    assert cli.main(["onboard", "questions", "--area", "steward", "--json"]) == 0
    printed = json.loads(capsys.readouterr().out)
    assert [a["area"] for a in printed["areas"]] == ["steward"]
    with pytest.raises(ops.OpsError):
        ops.onboard_questions("nope")
