import json
from pathlib import Path

import pytest

import jarvis.catalog
import jarvis.validation
from jarvis.catalog import (
    DEFAULT_AUTOCOMPACT_WINDOW,
    DEFAULT_COLD_PREFIX_FLOOR,
    DEFAULT_COLD_PREFIX_FLOOR_MAX,
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


def test_tool_search_defaults_and_overrides_per_project():
    """§4 of docs/specs/2026-10-02-serena-the-cheap-path.md: a three-state string enum,
    fleet-wide with a per-project override. The default PINS DEFERRAL ON — deferral is
    what makes a worker's first navigation call a symbol call (7/7 deferred against 1/10
    with the tools present, wo-ab5d81db), so `cli` would leave that outcome to a vendor
    default and `off` is a measured regression on it."""
    from jarvis.catalog import DEFAULT_WORKER_TOOL_SEARCH, VALID_TOOL_SEARCH

    assert DEFAULT_WORKER_TOOL_SEARCH == "on"
    assert VALID_TOOL_SEARCH == ("off", "on", "cli")

    cat = parse_catalog({"projects": [{"name": "a", "path": "/tmp/a"}]})
    assert cat.os.default_tool_search == "on"
    assert cat.projects[0].worker.tool_search == "on"

    cat = parse_catalog({
        "os": {"defaults": {"tool_search": "off"}},
        "projects": [
            {"name": "a", "path": "/tmp/a"},
            {"name": "b", "path": "/tmp/b", "worker": {"tool_search": "on"}},
        ],
    })
    assert cat.os.default_tool_search == "off"
    assert cat.projects[0].worker.tool_search == "off"      # inherits the fleet value
    assert cat.projects[1].worker.tool_search == "on"


def test_an_invalid_tool_search_names_the_key_and_the_valid_values():
    """The enum check IS the error message `jarvis config set` shows: `ops.set_config`
    re-parses the document to validate (spec §4)."""
    with pytest.raises(CatalogError) as e:
        parse_catalog({"projects": [{"name": "a", "path": "/x",
                                     "worker": {"tool_search": "true"}}]})
    assert "worker.tool_search" in str(e.value)
    for value in ("off", "on", "cli"):
        assert value in str(e.value)


def test_py_nav_hook_defaults_off_and_overrides_per_project():
    """§6 of docs/specs/2026-10-02-serena-the-cheap-path.md: TWO states, not three —
    `cli` exists only where Jarvis defers to a vendor behaviour, and this hook is
    entirely Jarvis's own. Default OFF: the hook measures ZERO contribution to first-call
    order, and on fleet-wide a worker cannot search the tree for ANY text (issue 936);
    wo-d2d777dc owns that fix and the flip is sequenced behind it (wo-ab5d81db)."""
    from jarvis.catalog import DEFAULT_WORKER_PY_NAV_HOOK, VALID_PY_NAV_HOOK

    assert DEFAULT_WORKER_PY_NAV_HOOK == "off"
    assert VALID_PY_NAV_HOOK == ("off", "on")

    cat = parse_catalog({"projects": [{"name": "a", "path": "/tmp/a"}]})
    assert cat.os.default_py_nav_hook == "off"
    assert cat.projects[0].worker.py_nav_hook == "off"

    cat = parse_catalog({
        "os": {"defaults": {"py_nav_hook": "on"}},
        "projects": [
            {"name": "a", "path": "/tmp/a"},
            {"name": "b", "path": "/tmp/b", "worker": {"py_nav_hook": "off"}},
        ],
    })
    assert cat.os.default_py_nav_hook == "on"
    assert cat.projects[0].worker.py_nav_hook == "on"       # inherits the fleet value
    assert cat.projects[1].worker.py_nav_hook == "off"


def test_an_invalid_py_nav_hook_names_the_key_and_the_valid_values():
    """The enum check IS the error message `jarvis config set` shows (spec §6)."""
    with pytest.raises(CatalogError) as e:
        parse_catalog({"projects": [{"name": "a", "path": "/x",
                                     "worker": {"py_nav_hook": "true"}}]})
    assert "worker.py_nav_hook" in str(e.value)
    for value in ("off", "on"):
        assert value in str(e.value)


def test_bash_first_defaults_off_and_overrides_per_project():
    """§1 of docs/superpowers/specs/2026-10-01-the-steer-that-beat-the-brief.md: a string
    enum, fleet-wide with a per-project override, and the DEFAULT DISABLES — `relaxed` is
    a softer copy of the instruction that already beat the brief at a measured 0% hit
    rate."""
    from jarvis.catalog import DEFAULT_WORKER_BASH_FIRST, VALID_BASH_FIRST

    assert DEFAULT_WORKER_BASH_FIRST == "off"
    assert VALID_BASH_FIRST == ("off", "relaxed", "strict", "cli")

    cat = parse_catalog({"projects": [{"name": "a", "path": "/tmp/a"}]})
    assert cat.os.default_bash_first == "off"
    assert cat.projects[0].worker.bash_first == "off"

    cat = parse_catalog({
        "os": {"defaults": {"bash_first": "relaxed"}},
        "projects": [
            {"name": "a", "path": "/tmp/a"},
            {"name": "b", "path": "/tmp/b", "worker": {"bash_first": "cli"}},
        ],
    })
    assert cat.os.default_bash_first == "relaxed"
    assert cat.projects[0].worker.bash_first == "relaxed"   # inherits the fleet value
    assert cat.projects[1].worker.bash_first == "cli"


@pytest.mark.parametrize("value", ["off", "relaxed", "strict", "cli"])
def test_every_bash_first_value_parses(value):
    cat = parse_catalog({"projects": [{"name": "a", "path": "/tmp/a",
                                       "worker": {"bash_first": value}}]})
    assert cat.projects[0].worker.bash_first == value


def test_an_invalid_bash_first_names_the_key_and_the_valid_values():
    """The enum check IS the error message `jarvis config set` shows: `ops.set_config`
    re-parses the document to validate (spec §1)."""
    with pytest.raises(CatalogError) as e:
        parse_catalog({"projects": [{"name": "a", "path": "/x",
                                     "worker": {"bash_first": "true"}}]})
    assert "worker.bash_first" in str(e.value)
    for value in ("off", "relaxed", "strict", "cli"):
        assert value in str(e.value)


@pytest.mark.parametrize("bad,msg", [
    ({"projects": "nope"}, "projects"),
    ({"projects": [{"name": "a", "path": "/x", "worker": {"bash_first": "yes"}}]},
     "bash_first"),
    ({"os": {"defaults": {"bash_first": "on"}}}, "os.defaults.bash_first"),
    ({"projects": [{"name": "a", "path": "/x", "worker": {"tool_search": "yes"}}]},
     "tool_search"),
    ({"os": {"defaults": {"tool_search": "relaxed"}}}, "os.defaults.tool_search"),
    ({"projects": [{"name": "a", "path": "/x", "worker": {"py_nav_hook": "cli"}}]},
     "py_nav_hook"),
    ({"os": {"defaults": {"py_nav_hook": "yes"}}}, "os.defaults.py_nav_hook"),
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
    assert v.diff_chars == 150000
    assert v.decision_record_chars == 6000
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


# -- the stakes classifier: three values, not a boolean --------------------------------


def test_the_stakes_classifier_ships_on_the_regex():
    """SS3.4. `regex` is today's behaviour exactly — no call, no new row, no new event —
    and it is the shipped default at both levels."""
    assert parse_catalog({"projects": []}).os.validation.stakes_classifier == "regex"
    assert validation_of({}).stakes_classifier == "regex"


@pytest.mark.parametrize("mode", ["regex", "shadow", "classifier"])
def test_every_legal_stakes_classifier_mode_parses(mode):
    assert validation_of({"stakes_classifier": mode}).stakes_classifier == mode


def test_a_fourth_stakes_classifier_value_is_refused_loudly():
    """The `roster` precedent: a typo that silently changes which net guards the settle
    path is refused at boot, naming the value and the three legal ones — not shrugged off
    to the default, which would read as the feature being off."""
    with pytest.raises(CatalogError) as err:
        validation_of({"stakes_classifier": "haiku"})
    message = str(err.value)
    assert "haiku" in message
    for legal in ("regex", "shadow", "classifier"):
        assert legal in message


def test_a_project_inherits_the_fleet_stakes_classifier_and_may_override_it():
    """The ordinary field-level fallback: a project naming one key keeps the rest."""
    quiet, loud = projects_validation({"stakes_classifier": "shadow"},
                                      {}, {"validation": {"auto_review": True}})
    assert quiet.stakes_classifier == "shadow"
    assert loud.stakes_classifier == "shadow"
    assert loud.auto_review is True

    [over] = projects_validation({"stakes_classifier": "shadow"},
                                 {"validation": {"stakes_classifier": "classifier"}})
    assert over.stakes_classifier == "classifier"


def test_the_confirmation_pass_has_its_own_diff_budget_and_never_reads_the_panels():
    """Two numbers, no shared name, no shared reader: spec
    docs/superpowers/specs/2026-09-26-bounded-model-inputs.md § 2."""
    v = parse_catalog({"projects": []}).os.validation
    assert v.confirm_diff_chars == 12000
    assert v.diff_chars == 150000

    [over] = projects_validation({"confirm_diff_chars": 9000},
                                 {"validation": {"confirm_diff_chars": 500}})
    assert over.confirm_diff_chars == 500
    assert over.diff_chars == 150000

    with pytest.raises(CatalogError,
                       match="os.validation.confirm_diff_chars must be >= 1"):
        validation_of({"confirm_diff_chars": 0})


@pytest.mark.parametrize("key", ["timeout", "max_rounds", "diff_chars",
                                 "confirm_diff_chars", "decision_record_chars"])
def test_a_validation_budget_below_one_is_rejected(key):
    """Zero rounds is a review that never runs while claiming to; zero diff_chars is a
    panel handed nothing, which the design says must never be asked to judge. Zero
    decision_record_chars is a reviewer shown none of the order's own rulings, which is
    the defect the record exists to fix."""
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
              "timeout": 90, "max_rounds": 1, "diff_chars": 200,
              "decision_record_chars": 400, "feature_units": False}
    [v] = projects_validation(os_raw, {"validation": {"max_rounds": 5}})
    assert v.max_rounds == 5
    assert v.enabled is True
    assert v.roster == ("tester", "chair")
    assert v.chair_model == "opus"
    assert v.timeout == 90
    assert v.diff_chars == 200
    assert v.decision_record_chars == 400
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


def test_the_cold_prefix_floor_is_fleet_wide_and_not_a_project_override():
    """Deliberately NOT under `os.defaults`, which is the namespace projects override.

    The two surfaces that consume this threshold walk transcripts rather than work
    orders, so a per-project value would be honoured for one order's bill and ignored by
    every aggregate. This pins the placement: a project naming the key must not change
    anything, or the knob would look overridable while behaving fleet-wide (Neo, q191).
    """
    c = parse_catalog({
        "os": {"cold_prefix_floor": 9_000},
        "projects": [{"name": "a", "path": ".", "cold_prefix_floor": 123}],
    })
    assert c.os.cold_prefix_floor == 9_000
    assert not hasattr(c.projects[0].worker, "cold_prefix_floor")


def test_the_cold_prefix_floor_is_validated_at_boot_not_at_report_time():
    """A bad value must fail where it was typed, not surface as a cost report quietly
    reclassifying every boundary — which reads as a finding, not as a config error."""
    assert (parse_catalog({"projects": []}).os.cold_prefix_floor
            == DEFAULT_COLD_PREFIX_FLOOR)
    for bad in ("5000", True, -1, DEFAULT_COLD_PREFIX_FLOOR_MAX + 1):
        with pytest.raises(CatalogError, match="cold_prefix_floor"):
            parse_catalog({"os": {"cold_prefix_floor": bad}, "projects": []})


def test_the_guard_rail_on_the_floor_is_itself_configurable():
    """`cold_prefix_floor_max` is config, not a constant: a fleet whose static heads are
    unusually large has to be able to raise the floor past the shipped ceiling."""
    over = {"cold_prefix_floor": DEFAULT_COLD_PREFIX_FLOOR_MAX + 50_000}
    with pytest.raises(CatalogError, match="cold_prefix_floor"):
        parse_catalog({"os": over, "projects": []})
    raised = parse_catalog({
        "os": {**over, "cold_prefix_floor_max": DEFAULT_COLD_PREFIX_FLOOR_MAX + 60_000},
        "projects": [],
    })
    assert raised.os.cold_prefix_floor == DEFAULT_COLD_PREFIX_FLOOR_MAX + 50_000
    with pytest.raises(CatalogError, match="cold_prefix_floor_max"):
        parse_catalog({"os": {"cold_prefix_floor_max": 0}, "projects": []})


def test_worker_require_crew_defaults_true():
    """§7 of docs/superpowers/specs/2026-09-23-the-crew-a-worker-must-use.md: the crew
    is the default, and turning it off is a project's deliberate opt-out."""
    cat = parse_catalog({"projects": [{"name": "a", "path": "/tmp/a"}]})
    assert cat.projects[0].worker.require_crew is True


def test_worker_require_crew_parsed():
    cat = parse_catalog({"projects": [
        {"name": "a", "path": "/tmp/a", "worker": {"require_crew": False}},
        {"name": "b", "path": "/tmp/b", "worker": {"require_crew": True}},
    ]})
    assert cat.projects[0].worker.require_crew is False
    assert cat.projects[1].worker.require_crew is True
    with pytest.raises(CatalogError, match="require_crew"):
        parse_catalog({"projects": [
            {"name": "a", "path": "/tmp/a", "worker": {"require_crew": "no"}}]})


# -- observability: what debug data is COLLECTED ----------------------------------------
#
# §10 of docs/specs/2026-09-24-order-observability.md. `off` gates exactly one write,
# §5's per-turn ingredient row, and no read.


def test_observability_ships_at_normal_fleet_wide_and_per_project():
    cat = parse_catalog({"projects": [{"name": "a", "path": "/tmp/a"}]})
    assert cat.os.observability.level == "normal"
    assert cat.projects[0].observability.level == "normal"


def test_a_project_inherits_the_fleet_observability_level_and_may_override_it():
    """`_parse_inspect`'s field-level inheritance: the project object is the ANSWER, so
    no caller consults two objects."""
    cat = parse_catalog({
        "os": {"observability": {"level": "off"}},
        "projects": [
            {"name": "a", "path": "/tmp/a"},
            {"name": "b", "path": "/tmp/b", "observability": {"level": "full"}},
        ],
    })
    assert cat.os.observability.level == "off"
    assert cat.projects[0].observability.level == "off"
    assert cat.projects[1].observability.level == "full"


def test_an_unknown_observability_level_is_refused_naming_the_legal_ones():
    """`GateConfig.parse`'s rule: a typo in `jarvis config set` must not silently leave
    debugging off."""
    with pytest.raises(CatalogError, match="off.*normal.*full"):
        parse_catalog({"os": {"observability": {"level": "verbose"}}, "projects": []})
    with pytest.raises(CatalogError, match=r"projects\[0\] \(a\).observability"):
        parse_catalog({"projects": [
            {"name": "a", "path": "/tmp/a", "observability": {"level": "loud"}}]})
    with pytest.raises(CatalogError, match="must be an object"):
        parse_catalog({"os": {"observability": "full"}, "projects": []})


# -- the backstop ceiling on an OS-side prompt (spec §4,
# docs/superpowers/specs/2026-09-26-bounded-model-inputs.md) -------------------------


def test_the_os_prompt_ceiling_defaults_to_the_measured_number():
    """400,000 is a MEASURED default: the worst legitimate OS call is 285,929 chars."""
    from jarvis.catalog import DEFAULT_MAX_OS_PROMPT_CHARS

    assert DEFAULT_MAX_OS_PROMPT_CHARS == 400_000
    assert parse_catalog({"projects": []}).os.max_os_prompt_chars == 400_000
    assert parse_catalog({"os": {"max_os_prompt_chars": 500_000},
                          "projects": []}).os.max_os_prompt_chars == 500_000


def test_a_ceiling_below_the_floor_is_refused_at_boot():
    """Below the floor the ceiling silently disables validation, which is worse than
    the bug it fixes — so it fails where it was typed."""
    from jarvis.catalog import MAX_OS_PROMPT_CHARS_MIN

    assert MAX_OS_PROMPT_CHARS_MIN == 300_000
    with pytest.raises(CatalogError) as caught:
        parse_catalog({"os": {"max_os_prompt_chars": 150_000}, "projects": []})
    msg = str(caught.value).replace(",", "")
    assert "150000" in msg
    assert str(MAX_OS_PROMPT_CHARS_MIN) in msg
    assert "validation" in msg
    for bad in ("400000", True, 0, -1, MAX_OS_PROMPT_CHARS_MIN - 1):
        with pytest.raises(CatalogError, match="max_os_prompt_chars"):
            parse_catalog({"os": {"max_os_prompt_chars": bad}, "projects": []})


def test_the_knowledge_hint_bounds_round_trip():
    """Spec test 15 of 2026-10-02-learn-search-returns-an-index.md — a pointer, not a
    second index, so the defaults are deliberately small and separate from the digest."""
    from jarvis.catalog import OsConfig

    assert (OsConfig().knowledge_hint_limit, OsConfig().knowledge_hint_chars) == (3, 400)
    cat = parse_catalog({"os": {"knowledge_hint_limit": 5, "knowledge_hint_chars": 900},
                         "projects": []})
    assert (cat.os.knowledge_hint_limit, cat.os.knowledge_hint_chars) == (5, 900)
    # and the digest budget is untouched by either
    assert cat.os.knowledge_digest_chars == 4000


def test_the_ceiling_has_no_off_switch():
    """A backstop with an off switch is not a backstop: null is refused, not honoured."""
    with pytest.raises(CatalogError, match="max_os_prompt_chars"):
        parse_catalog({"os": {"max_os_prompt_chars": None}, "projects": []})


def test_loading_a_catalog_arms_the_transport_ceiling(tmp_path):
    """The seam Neo ruled on (q1077): one override at startup, not a parameter plumbed
    through twenty call sites."""
    from jarvis import claude_cli

    before = claude_cli.MAX_OS_PROMPT_CHARS
    f = tmp_path / "c.json"
    f.write_text(json.dumps({"os": {"max_os_prompt_chars": 450_000}, "projects": []}))
    try:
        load_catalog(f)
        assert claude_cli.MAX_OS_PROMPT_CHARS == 450_000
    finally:
        claude_cli.set_max_os_prompt_chars(before)


def test_the_blocker_set_is_the_catalogs_to_narrow(tmp_path):
    """`supervisor.health_reassert_blockers` is a CATALOG SETTING and never a module
    constant (kn-1cec46b5, Neo q1217). An unknown id is refused with the known ones
    named — `_parse_remedies`' rule — and a project overrides the list field by field
    while the rest of `os.supervisor` is inherited.

    It also pins the `_SUPERVISOR_NON_NUMERIC` trap: a field missing from that set is a
    `TypeError` on every catalog load, which this test is the first to see.
    """
    from jarvis import health

    with pytest.raises(CatalogError, match=", ".join(health.BLOCKERS)):
        parse_catalog({"os": {"supervisor": {
            "health_reassert_blockers": ["the-weather"]}}, "projects": []})
    with pytest.raises(CatalogError, match="list of blocker ids"):
        parse_catalog({"os": {"supervisor": {
            "health_reassert_blockers": "dependency"}}, "projects": []})

    cat = parse_catalog({"os": {"supervisor": {"health_stale_minutes": 90,
                                              "health_reassert_blockers": ["user"]}},
                         "projects": [{"name": "p", "path": str(tmp_path)},
                                      {"name": "q", "path": str(tmp_path),
                                       "supervisor": {"health_reassert_blockers":
                                                      ["dependency"]}}]})
    assert cat.os.supervisor.health_reassert_blockers == ("user",)
    assert cat.projects[0].supervisor.health_reassert_blockers == ("user",)
    assert cat.projects[1].supervisor.health_reassert_blockers == ("dependency",)
    assert cat.projects[1].supervisor.health_stale_minutes == 90, (
        "a project naming the blockers keeps the fleet's answer for everything else")


def test_the_sweep_cadence_defaults_do_not_move(tmp_path):
    """The free re-assertion buys quiet by SPENDING less, never by watching less — the
    spec's "out of scope" list, and the alternative it rejected."""
    cfg = parse_catalog({"os": {}, "projects": []}).os.supervisor
    assert (cfg.health_every_ticks, cfg.health_min_interval_minutes,
            cfg.health_stale_minutes, cfg.health_max_units_per_tick) == (20, 30, 720, 4)


# -- navigation: the classifier's patterns are DATA, never module constants -------------
#
# q1216's condition, §2.3 of
# docs/superpowers/specs/2026-10-02-subagent-cache-anatomy-and-the-navigation-split.md:
# re-measuring under a different definition of "navigation" must not need a release.


def test_navigation_ships_the_measured_defaults_fleet_wide_and_per_project():
    from jarvis.catalog import (
        DEFAULT_NAVIGATION_BASH_COMMANDS,
        DEFAULT_NAVIGATION_CODE_SUFFIXES,
        DEFAULT_NAVIGATION_SYMBOL_TOOLS,
        DEFAULT_NAVIGATION_TEXT_SEARCH_TOOLS,
        DEFAULT_NAVIGATION_WINDOW_DAYS,
    )

    cat = parse_catalog({"projects": [{"name": "a", "path": "/tmp/a"}]})

    assert DEFAULT_NAVIGATION_BASH_COMMANDS == ("cat", "head", "sed", "grep", "rg",
                                                "find")
    assert DEFAULT_NAVIGATION_TEXT_SEARCH_TOOLS == ("Grep", "Glob")
    assert DEFAULT_NAVIGATION_CODE_SUFFIXES == (".py",)
    assert DEFAULT_NAVIGATION_WINDOW_DAYS == 7
    # text search with a Serena name is NOT a symbol call (kn-a397fb52)
    assert "search_for_pattern" not in DEFAULT_NAVIGATION_SYMBOL_TOOLS
    assert cat.os.navigation.bash_commands == DEFAULT_NAVIGATION_BASH_COMMANDS
    assert cat.projects[0].navigation.symbol_tools == DEFAULT_NAVIGATION_SYMBOL_TOOLS
    assert cat.projects[0].navigation.enabled is True


def test_a_project_naming_one_navigation_key_inherits_the_rest():
    """`_parse_inspect`'s field-level inheritance: the project object is the ANSWER, so
    no caller consults two objects."""
    cat = parse_catalog({
        "os": {"navigation": {"window_days": 30}},
        "projects": [
            {"name": "a", "path": "/tmp/a"},
            {"name": "b", "path": "/tmp/b",
             "navigation": {"bash_commands": ["cat", "rg"]}},
        ],
    })

    assert cat.os.navigation.window_days == 30
    assert cat.projects[0].navigation.window_days == 30
    assert cat.projects[1].navigation.window_days == 30
    assert cat.projects[1].navigation.bash_commands == ("cat", "rg")
    assert cat.projects[1].navigation.code_suffixes == (".py",)


def test_an_empty_navigation_pattern_list_is_refused_naming_the_key():
    """An empty classifier reports 0% everywhere and looks like a win."""
    with pytest.raises(CatalogError, match="navigation.bash_commands"):
        parse_catalog({"os": {"navigation": {"bash_commands": []}}, "projects": []})
    with pytest.raises(CatalogError, match=r"projects\[0\] \(a\).navigation"):
        parse_catalog({"projects": [
            {"name": "a", "path": "/tmp/a", "navigation": {"symbol_tools": []}}]})


def test_a_navigation_pattern_list_that_is_not_strings_is_refused():
    with pytest.raises(CatalogError, match="must be a list of strings"):
        parse_catalog({"os": {"navigation": {"bash_commands": "cat"}}, "projects": []})
    with pytest.raises(CatalogError, match="must be a list of strings"):
        parse_catalog({"os": {"navigation": {"text_search_tools": [1, 2]}},
                       "projects": []})
    with pytest.raises(CatalogError, match="must be an object"):
        parse_catalog({"os": {"navigation": "on"}, "projects": []})


def test_a_suffix_without_a_leading_dot_and_a_zero_window_are_refused():
    with pytest.raises(CatalogError, match="code_suffixes"):
        parse_catalog({"os": {"navigation": {"code_suffixes": ["py"]}},
                       "projects": []})
    with pytest.raises(CatalogError, match="window_days"):
        parse_catalog({"os": {"navigation": {"window_days": 0}}, "projects": []})


# -- `os.cost`: the fleet distribution's tunables ---------------------------------------
#
# §6 of docs/superpowers/specs/2026-10-06-fleet-cost-distribution.md. Neo's rider: no
# module constant for anything tunable, so the usage-week reset and the percentile are
# catalog settings, resolvable fleet-wide AND per project.


def test_cost_defaults_ship_on_both_config_objects(tmp_path):
    cat = parse_catalog({"projects": [{"name": "a", "path": str(tmp_path)}]})
    for cfg in (cat.os.cost, cat.projects[0].cost):
        assert cfg.week_reset_weekday == 0            # Monday
        assert cfg.week_reset_hour == 21
        assert cfg.week_reset_zone == "America/Los_Angeles"
        assert cfg.percentile == 0.9
        assert cfg.max_orders == 500
        # §10.10 of the per-tool addendum: the `chars` estimator's divisor and the row
        # cap of the tool table, both catalog settings for the same stated reason.
        assert cfg.chars_per_token == 4.0
        assert cfg.tool_rows == 20
        # §7 of docs/superpowers/specs/2026-10-07-cost-window-selector.md: the 5h grid's
        # length is a belief about the usage grid, so it is a setting and not a constant.
        assert cfg.session_window_hours == 5.0


def test_a_non_positive_session_window_hours_is_refused():
    """A fractional length is a legal belief about the grid; zero is not a length."""
    for bad in (0, -1, -2.5):
        with pytest.raises(CatalogError, match="session_window_hours must be > 0"):
            parse_catalog({"os": {"cost": {"session_window_hours": bad}},
                           "projects": []})
    assert parse_catalog({"os": {"cost": {"session_window_hours": 2.5}},
                          "projects": []}).os.cost.session_window_hours == 2.5


def test_a_non_positive_chars_per_token_is_refused():
    """Zero or less is not a divisor, and a negative one would report negative tokens."""
    for bad in (0, -1, -0.5):
        with pytest.raises(CatalogError, match="chars_per_token"):
            parse_catalog({"os": {"cost": {"chars_per_token": bad}}, "projects": []})
    assert parse_catalog({"os": {"cost": {"chars_per_token": 3.5}},
                          "projects": []}).os.cost.chars_per_token == 3.5


def test_a_cost_tool_rows_below_one_is_refused():
    """Zero rows is a table with a truncation line and nothing above it."""
    for bad in (0, -5):
        with pytest.raises(CatalogError, match="tool_rows"):
            parse_catalog({"os": {"cost": {"tool_rows": bad}}, "projects": []})
    assert parse_catalog({"os": {"cost": {"tool_rows": 1}},
                          "projects": []}).os.cost.tool_rows == 1


def test_a_project_overrides_one_cost_key_and_inherits_the_rest(tmp_path):
    """Field-level inheritance, `_parse_inspect`'s shape (kn-6ca2bcd9): the project
    object is the ANSWER, so no caller consults two objects."""
    from jarvis import config_version

    cat = parse_catalog({
        "os": {"cost": {"percentile": 0.95, "max_orders": 50}},
        "projects": [
            {"name": "a", "path": str(tmp_path)},
            {"name": "b", "path": str(tmp_path), "cost": {"percentile": 0.5}},
        ],
    })
    assert cat.os.cost.percentile == 0.95
    assert cat.projects[0].cost.percentile == 0.95          # inherited from os
    assert cat.projects[1].cost.percentile == 0.5           # its own
    assert cat.projects[1].cost.max_orders == 50            # inherited from os
    assert cat.projects[1].cost.week_reset_hour == 21       # inherited from the default
    resolved = config_version.resolve(cat)
    assert resolved["os.cost.percentile"] == 0.95
    assert resolved["projects.b.cost.percentile"] == 0.5
    assert resolved["os.cost.week_reset_zone"] == "America/Los_Angeles"


def test_midnight_is_a_legal_cost_week_reset_hour():
    """Zero is legal here: `_parse_inspect`'s ">= 1" rule would reject midnight, and
    an hour is not a count."""
    cat = parse_catalog({"os": {"cost": {"week_reset_hour": 0,
                                         "week_reset_weekday": 0}}, "projects": []})
    assert cat.os.cost.week_reset_hour == 0
    assert cat.os.cost.week_reset_weekday == 0


def test_an_out_of_range_cost_reset_day_or_hour_is_refused_naming_the_key():
    for bad in (-1, 7, 99):
        with pytest.raises(CatalogError, match="week_reset_weekday"):
            parse_catalog({"os": {"cost": {"week_reset_weekday": bad}}, "projects": []})
    for bad in (-1, 24, 100):
        with pytest.raises(CatalogError, match="week_reset_hour"):
            parse_catalog({"os": {"cost": {"week_reset_hour": bad}}, "projects": []})


def test_a_cost_percentile_outside_the_open_interval_is_refused():
    """Both ends exclusive: 0 names no value and 1 is the max, which `max` already is."""
    for bad in (0, 1, -0.5, 1.5):
        with pytest.raises(CatalogError, match="percentile"):
            parse_catalog({"os": {"cost": {"percentile": bad}}, "projects": []})
    assert parse_catalog({"os": {"cost": {"percentile": 0.99}},
                          "projects": []}).os.cost.percentile == 0.99


def test_a_cost_max_orders_below_one_is_refused():
    for bad in (0, -5):
        with pytest.raises(CatalogError, match="max_orders"):
            parse_catalog({"os": {"cost": {"max_orders": bad}}, "projects": []})


def test_an_unknown_cost_time_zone_is_refused_naming_the_bad_value(tmp_path):
    """A zone that no `ZoneInfo` can construct makes every window wrong, so it fails
    where it was typed."""
    with pytest.raises(CatalogError) as caught:
        parse_catalog({"os": {"cost": {"week_reset_zone": "Mars/Olympus"}},
                       "projects": []})
    assert "Mars/Olympus" in str(caught.value)
    assert "week_reset_zone" in str(caught.value)
    with pytest.raises(CatalogError, match=r"projects\[0\] \(a\).cost"):
        parse_catalog({"projects": [{"name": "a", "path": str(tmp_path),
                                     "cost": {"week_reset_zone": "Nowhere/At_All"}}]})
    with pytest.raises(CatalogError, match="must be an object"):
        parse_catalog({"os": {"cost": "weekly"}, "projects": []})


def test_cost_paths_have_an_explicit_apply_class():
    """ops.APPLY_RULES decides it rather than falling through (spec §6.2)."""
    from jarvis import ops

    assert any(glob == "*.cost.*" for glob, _ in ops.APPLY_RULES)
    assert ops.apply_class("os.cost.percentile") == "hot"
    assert ops.apply_class("projects.a.cost.week_reset_hour") == "hot"


def test_the_feature_merge_wait_resolves_per_project_and_falls_back_fleet_wide():
    """§4 of
    docs/superpowers/specs/2026-10-07-a-feature-round-must-judge-a-head-that-contains-its-children.md:
    how long the reconciler waits for a merged child's commit to appear on the default
    branch is a per-project habit, so it is a catalog key with this block's ordinary
    field-level fallback rather than a module constant."""
    assert (parse_catalog({"projects": []}).os.validation.feature_merge_wait_minutes
            == jarvis.catalog.DEFAULT_VALIDATION_FEATURE_MERGE_WAIT_MINUTES)

    inherits, own = projects_validation(
        {"feature_merge_wait_minutes": 30}, {},
        {"validation": {"feature_merge_wait_minutes": 5}})
    assert inherits.feature_merge_wait_minutes == 30
    assert own.feature_merge_wait_minutes == 5

    [silent] = projects_validation(None, {})
    assert (silent.feature_merge_wait_minutes
            == jarvis.catalog.DEFAULT_VALIDATION_FEATURE_MERGE_WAIT_MINUTES)

    # 0 is legal and means "never defer": check once, flag immediately.
    assert validation_of(
        {"feature_merge_wait_minutes": 0}).feature_merge_wait_minutes == 0
    with pytest.raises(CatalogError,
                       match="os.validation.feature_merge_wait_minutes must be >= 0"):
        validation_of({"feature_merge_wait_minutes": -1})
