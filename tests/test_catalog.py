import json
from pathlib import Path

import pytest

import jarvis.catalog
import jarvis.validation
from jarvis.catalog import (
    DEFAULT_AUTOCOMPACT_WINDOW,
    CatalogError,
    load_catalog,
    parse_catalog,
)
from jarvis.neo_store import SEATS
from jarvis.project_store import VALIDATOR_SEATS


def test_minimal_catalog(tmp_path):
    f = tmp_path / "c.json"
    f.write_text(json.dumps({"projects": [{"name": "a", "path": str(tmp_path)}]}))
    cat = load_catalog(f)
    assert cat.os.default_model == "claude-opus-5"
    assert cat.projects[0].name == "a"
    assert cat.projects[0].worker.model == "claude-opus-5"
    assert cat.projects[0].worker.permission_mode == "auto"
    assert cat.projects[0].max_concurrent == 5


def test_project_overrides_inherit():
    cat = parse_catalog({
        "os": {"defaults": {"model": "opus", "permission_mode": "auto"}},
        "projects": [
            {"name": "a", "path": "/tmp/a"},
            {"name": "b", "path": "/tmp/b", "model": "haiku",
             "worker": {"permission_mode": "plan"}},
        ],
    })
    assert cat.projects[0].worker.model == "opus"
    assert cat.projects[0].worker.permission_mode == "auto"
    assert cat.projects[1].worker.model == "haiku"
    assert cat.projects[1].worker.permission_mode == "plan"


def test_max_concurrent_config():
    cat = parse_catalog({
        "os": {"defaults": {"max_concurrent": 3}},
        "projects": [
            {"name": "a", "path": "/tmp/a"},                     # inherits fleet default
            {"name": "b", "path": "/tmp/b", "max_concurrent": 8},  # per-project override
        ],
    })
    assert cat.os.default_max_concurrent == 3
    assert cat.projects[0].max_concurrent == 3
    assert cat.projects[1].max_concurrent == 8


def test_autocompact_is_bounded_by_default():
    """The context bound is ON for a catalog that says nothing about it.

    This is the whole point of the setting: cache READ is 56% of the fleet's bill and
    it is linear in context size, so a default of "no bound" leaves the bleed running
    until someone remembers a flag.
    """
    cat = parse_catalog({"projects": [{"name": "a", "path": "/tmp/a"}]})
    # The literal, not the constant: asserting the constant against itself would hold
    # even if someone set it to None, which is the one change this test exists to catch.
    assert cat.os.default_autocompact_window == 400_000
    assert cat.projects[0].worker.autocompact_window == 400_000
    assert DEFAULT_AUTOCOMPACT_WINDOW == 400_000


def test_autocompact_fleet_default_and_project_override():
    cat = parse_catalog({
        "os": {"defaults": {"autocompact_window": 200_000}},
        "projects": [
            {"name": "a", "path": "/tmp/a"},                       # inherits the fleet
            {"name": "b", "path": "/tmp/b",
             "worker": {"autocompact_window": 600_000}},           # raises it
        ],
    })
    # None of the three is DEFAULT_AUTOCOMPACT_WINDOW: an override that silently fell
    # back to the module default would otherwise pass this test.
    assert cat.os.default_autocompact_window == 200_000
    assert cat.projects[0].worker.autocompact_window == 200_000
    assert cat.projects[1].worker.autocompact_window == 600_000


def test_a_project_opts_out_with_an_explicit_null():
    """null is the opt-out, and it must survive a non-null fleet default.

    The `or`-style fallback that reads fine for model and effort is wrong here: 0 and
    None both mean "no bound", so a project's null has to beat the fleet's number
    rather than fall through to it.
    """
    cat = parse_catalog({
        "os": {"defaults": {"autocompact_window": 150_000}},
        "projects": [{"name": "a", "path": "/tmp/a",
                      "worker": {"autocompact_window": None}}],
    })
    assert cat.projects[0].worker.autocompact_window is None


def test_absent_and_null_are_different():
    """Silence inherits the bound; only an explicit null removes it."""
    cat = parse_catalog({
        "projects": [
            {"name": "a", "path": "/tmp/a", "worker": {}},
            {"name": "b", "path": "/tmp/b", "worker": {"autocompact_window": None}},
        ],
    })
    assert cat.projects[0].worker.autocompact_window == 400_000
    assert cat.projects[1].worker.autocompact_window is None


@pytest.mark.parametrize("bad,msg", [
    ({"projects": "nope"}, "projects"),
    ({"projects": [{"path": "/x"}]}, "name"),
    ({"projects": [{"name": "a"}]}, "path"),
    ({"projects": [{"name": "a", "path": "/x"}, {"name": "a", "path": "/y"}]}, "duplicate"),
    ({"projects": [{"name": "a", "path": "/x", "worker": {"permission_mode": "yolo"}}]}, "permission_mode"),
    ({"projects": [{"name": "a", "path": "/x", "max_concurrent": 0}]}, "max_concurrent"),
    # Outside the range `claude --autocompact` accepts: caught at boot, not on the
    # first dispatch, because the CLI's rejection would surface as a dead worker.
    ({"os": {"defaults": {"autocompact_window": 50_000}}}, "autocompact_window"),
    ({"os": {"defaults": {"autocompact_window": 2_000_000}}}, "autocompact_window"),
    ({"projects": [{"name": "a", "path": "/x",
                    "worker": {"autocompact_window": 99_999}}]}, "autocompact_window"),
    ({"projects": [{"name": "a", "path": "/x",
                    "worker": {"autocompact_window": "150k"}}]}, "autocompact_window"),
])
def test_invalid_catalogs(bad, msg):
    with pytest.raises(CatalogError, match=msg):
        parse_catalog(bad)


def test_missing_file(tmp_path):
    with pytest.raises(CatalogError, match="not found"):
        load_catalog(tmp_path / "nope.json")


def test_unknown_project_lookup():
    cat = parse_catalog({"projects": [{"name": "a", "path": "/x"}]})
    with pytest.raises(CatalogError, match="unknown project"):
        cat.project("zzz")


def test_empty_projects_allowed():
    # A standby instance (e.g. a fresh production deployment) boots empty.
    cat = parse_catalog({"projects": []})
    assert cat.projects == []


def test_missing_projects_defaults_empty():
    cat = parse_catalog({"os": {"defaults": {"model": "sonnet"}}})
    assert cat.projects == []


# -- Neo's panel ---------------------------------------------------------------------


def panel_of(raw):
    return parse_catalog({"os": {"neo": {"panel": raw}}, "projects": []}).os.neo.panel


def test_the_panel_is_off_unless_a_catalog_turns_it_on():
    """The rule every work order in this feature obeys. Enabling it is a catalog edit,
    gated on a measurement that does not exist yet — never a default that drifts in."""
    assert parse_catalog({"projects": []}).os.neo.panel.enabled is False


def test_a_catalog_with_no_panel_key_parses():
    cat = parse_catalog({"os": {"neo": {"model": "opus"}}, "projects": []})
    assert cat.os.neo.panel.roster == ("premise", "chair")
    assert cat.os.neo.panel.kinds == ("question", "approval")


def test_an_empty_panel_block_parses():
    assert panel_of({}).enabled is False


def test_a_roster_of_every_seat_parses():
    """The negative control for the validator below: `neo_store.SEATS` is the vocabulary,
    and every name in it must be accepted — including the seats whose definitions ship in
    a later release. A config written ahead of the code is caught at run time (the seat
    records a `failed` opinion and the panel proceeds), not by refusing to boot the
    fleet."""
    assert panel_of({"roster": list(SEATS)}).roster == SEATS


def test_a_roster_naming_an_unknown_seat_is_rejected():
    """Every seat past `premise` is a safety check, so a typo that silently drops one
    removes a check and tells nobody — the same reasoning as an invalid
    `permission_mode`, with more at stake."""
    with pytest.raises(CatalogError, match="scpetic"):
        panel_of({"roster": ["premise", "scpetic", "chair"]})


def test_a_seat_model_for_an_unknown_seat_is_rejected():
    with pytest.raises(CatalogError, match="chiar"):
        panel_of({"seat_models": {"chiar": "haiku"}})


def test_a_panel_kind_that_is_not_a_question_kind_is_rejected():
    with pytest.raises(CatalogError, match="approvals"):
        panel_of({"kinds": ["question", "approvals"]})


def test_the_panel_block_must_be_an_object():
    with pytest.raises(CatalogError, match="os.neo.panel"):
        parse_catalog({"os": {"neo": {"panel": "yes please"}}, "projects": []})


def test_panel_settings_round_trip():
    p = panel_of({"enabled": True, "roster": ["premise", "blast", "chair"],
                  "seat_models": {"premise": "haiku"}, "chair_model": "opus",
                  "timeout": 90, "kinds": ["approval"], "fast_path": False})
    assert p.enabled is True
    assert p.roster == ("premise", "blast", "chair")
    assert p.seat_models == {"premise": "haiku"}
    assert p.chair_model == "opus"
    assert p.timeout == 90
    assert p.kinds == ("approval",)
    assert p.fast_path is False


# -- the validation panel -------------------------------------------------------------


def validation_of(raw):
    return parse_catalog({"os": {"validation": raw}, "projects": []}).os.validation


def test_validation_ships_disabled_with_every_default_spelled_out():
    """All eight by value, not by shape.

    `enabled` is the load-bearing one — at this default the OS must behave exactly as
    it does today — but each of the others prices a round, and a default that drifted
    would change fleet-wide spend without anyone editing a catalog.
    """
    v = parse_catalog({"projects": []}).os.validation
    assert v.enabled is False
    assert v.roster == ("tester", "security", "architect", "maintainer", "chair")
    assert v.seat_models == {}
    assert v.chair_model == ""
    assert v.timeout == 300
    assert v.max_rounds == 3
    assert v.diff_chars == 60000
    assert v.feature_units is True
    # and an empty block is the same thing as no block at all
    assert validation_of({}) == v


def test_a_validator_roster_naming_an_unknown_seat_is_rejected_and_the_five_are_not():
    """Paired on purpose. A validator strict enough to catch the typo is easy to write
    and easy to write too strictly, and the failure mode of "too strict" is a fleet that
    refuses to start — so the five legal names must be proved accepted in the same
    breath as the illegal one is refused."""
    assert validation_of({"roster": list(VALIDATOR_SEATS)}).roster == VALIDATOR_SEATS
    with pytest.raises(CatalogError, match="tetser"):
        validation_of({"roster": ["tetser", "chair"]})
    with pytest.raises(CatalogError, match="scurity"):
        validation_of({"seat_models": {"scurity": "haiku"}})


def test_a_roster_naming_a_seat_whose_markdown_has_not_shipped_still_parses(monkeypatch,
                                                                            tmp_path):
    """`VALIDATOR_SEATS` is the VOCABULARY, not the set of seats shipped in this build.

    All five now ship, so the staging case this test was written for — config written
    ahead of the code — has to be STAGED rather than found: the seat directory is swapped
    for an empty one, and parsing must still accept the five defaults. If it consulted
    the asset directory, a catalog written for the next release would stop the fleet
    booting on this one.
    """
    monkeypatch.setattr(jarvis.validation, "SEAT_ASSETS", tmp_path / "no-seats")
    assert jarvis.validation.shipped_seats() == ()

    assert validation_of({"enabled": True}).roster == VALIDATOR_SEATS


def test_the_validation_block_must_be_an_object():
    with pytest.raises(CatalogError, match="os.validation"):
        parse_catalog({"os": {"validation": "yes please"}, "projects": []})


def test_validation_settings_round_trip():
    v = validation_of({"enabled": True, "roster": ["tester", "chair"],
                       "seat_models": {"tester": "haiku"}, "chair_model": "opus",
                       "timeout": 90, "max_rounds": 1, "diff_chars": 200,
                       "feature_units": False})
    assert v.enabled is True
    assert v.roster == ("tester", "chair")
    assert v.seat_models == {"tester": "haiku"}
    assert v.chair_model == "opus"
    assert v.timeout == 90
    assert v.max_rounds == 1
    assert v.diff_chars == 200
    assert v.feature_units is False


@pytest.mark.parametrize("key", ["timeout", "max_rounds", "diff_chars"])
def test_a_validation_budget_below_one_is_rejected(key):
    """Zero rounds is a review that never runs while claiming to; zero diff_chars is a
    panel handed nothing, which the design says must never be asked to judge."""
    with pytest.raises(CatalogError, match=f"os.validation.{key}"):
        validation_of({key: 0})


# -- a project's own validation settings ----------------------------------------------


def projects_validation(os_raw=None, *project_raws):
    """Parse a fleet and hand back each project's resolved `validation`."""
    cat = parse_catalog({
        "os": {"validation": os_raw} if os_raw is not None else {},
        "projects": [dict({"name": chr(ord("a") + i), "path": "/tmp/p"}, **raw)
                     for i, raw in enumerate(project_raws)],
    })
    return [p.validation for p in cat.projects]


def test_a_project_that_says_nothing_gets_the_os_answer_and_ships_disabled():
    """The whole point of the default, restated one level down: adding the key must not
    turn anything on, and a silent project must be indistinguishable from today."""
    [quiet] = projects_validation(None, {})
    assert quiet == parse_catalog({"projects": []}).os.validation
    assert quiet.enabled is False

    [inherits] = projects_validation({"enabled": True, "max_rounds": 7}, {})
    assert inherits.enabled is True
    assert inherits.max_rounds == 7


def test_one_project_can_turn_validation_on_while_the_fleet_stays_off():
    """The acceptance criterion of the design doc's §1.2, as one assertion."""
    on, off = projects_validation(None, {"validation": {"enabled": True}}, {})
    assert on.enabled is True
    assert off.enabled is False


def test_a_project_override_inherits_every_key_it_does_not_name():
    """`os.validation` is the BASE, not a fallback consulted later: a project naming one
    key must carry the OS's answer for the other seven, so no caller has two objects to
    reconcile."""
    os_raw = {"enabled": True, "roster": ["tester", "chair"], "chair_model": "opus",
              "timeout": 90, "max_rounds": 1, "diff_chars": 200, "feature_units": False}
    [v] = projects_validation(os_raw, {"validation": {"max_rounds": 5}})
    assert v.max_rounds == 5
    assert v.enabled is True
    assert v.roster == ("tester", "chair")
    assert v.chair_model == "opus"
    assert v.timeout == 90
    assert v.diff_chars == 200
    assert v.feature_units is False


def test_a_project_seat_models_replaces_the_os_map_rather_than_merging_into_it():
    """Inheritance is FIELD-level, and `seat_models` is one field (Neo, q174). A project
    that names it owns the whole map; the seats it drops fall back to the project model
    the way an empty map always has."""
    [v] = projects_validation({"seat_models": {"architect": "opus"}},
                              {"validation": {"seat_models": {"chair": "sonnet"}}})
    assert v.seat_models == {"chair": "sonnet"}


def test_a_project_override_does_not_reach_back_up_to_the_os_block():
    cat = parse_catalog({
        "os": {"validation": {"enabled": False, "max_rounds": 3}},
        "projects": [{"name": "a", "path": "/tmp/a",
                      "validation": {"enabled": True, "max_rounds": 9}}],
    })
    assert cat.os.validation.enabled is False
    assert cat.os.validation.max_rounds == 3
    assert cat.projects[0].validation.enabled is True
    assert cat.projects[0].validation.max_rounds == 9


def test_a_project_validation_block_is_validated_and_the_error_names_the_project():
    """Same checks as the OS block — the vocabulary and the budgets — but a message that
    points at the object the user actually typed, not at `os.validation`."""
    with pytest.raises(CatalogError, match=r"projects\[0\] \(a\)\.validation\.roster"):
        projects_validation(None, {"validation": {"roster": ["tetser"]}})
    with pytest.raises(CatalogError, match=r"projects\[0\] \(a\)\.validation\.max_rounds"):
        projects_validation(None, {"validation": {"max_rounds": 0}})
    with pytest.raises(CatalogError, match=r'"projects\[0\] \(a\)\.validation"'):
        projects_validation(None, {"validation": "yes please"})
