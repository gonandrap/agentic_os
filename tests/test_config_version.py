"""The config version ledger: canonical form, content-addressed ids, resolution.

docs/superpowers/specs/2026-08-27-the-config-console.md §2, §9.

The index on `os_config_versions` is asserted in `tests/test_schema_upgrade.py`, not
here: a new table arrives free on `CREATE TABLE IF NOT EXISTS` but its indexes only if
they are in `SCHEMA`, and that file is where the upgrade path is proved.
"""

from __future__ import annotations

import fnmatch
import json

import pytest

from jarvis import catalog, config_version as cv
from jarvis.central_store import CentralStore


@pytest.fixture()
def store() -> CentralStore:
    return CentralStore()  # `jarvis_home` is autouse; this is per-test state


# --- canonical form and the content-addressed id ------------------------------------

def test_the_id_ignores_key_order_and_whitespace_but_not_a_value():
    """Content addressing is the whole mechanism: it is what makes "an edit that changes
    nothing writes no row" true rather than a special case somebody has to remember."""
    a = {"os": {"neo": {"enabled": True}, "defaults": {"model": "opus"}}}
    reordered = {"defaults": {"model": "opus"}, "neo": {"enabled": True}}
    b = {"os": reordered}
    reflowed = json.loads(json.dumps(a, indent=8))

    assert cv.version_id(a) == cv.version_id(b) == cv.version_id(reflowed)
    # ... and the negative half, without which an id that hashed nothing would pass
    changed = {"os": {"neo": {"enabled": False}, "defaults": {"model": "opus"}}}
    assert cv.version_id(changed) != cv.version_id(a)


def test_the_id_is_the_documented_shape():
    vid = cv.version_id({})
    assert vid.startswith("cfg-")
    assert len(vid) == len("cfg-") + 16
    int(vid[4:], 16)  # hex, or this raises


def test_the_canonical_form_is_sorted_and_two_space_indented():
    text = cv.canonicalise({"b": 1, "a": {"d": 2, "c": 3}})
    assert text == '{\n  "a": {\n    "c": 3,\n    "d": 2\n  },\n  "b": 1\n}'
    # A file written as `canonicalise(doc) + "\n"` re-canonicalises to the same string,
    # which is what lets `jarvis config adopt` compare a hand-edited file by hash.
    assert cv.canonicalise(json.loads(text + "\n")) == text


# --- resolve ------------------------------------------------------------------------

def test_resolve_materialises_every_default_the_document_never_mentions():
    """The argument that forces whole-snapshot storage (§2): a release that moves a
    default must not retroactively change what a stored version meant."""
    resolved = cv.resolve(catalog.parse_catalog({}))

    assert resolved["os.defaults.autocompact_window"] == 400000
    assert resolved["os.defaults.model"] == catalog.DEFAULT_MODEL
    assert resolved["os.validation.enabled"] is False
    assert resolved["os.validation.max_rounds"] == 3
    assert resolved["os.neo.panel.roster"] == list(catalog.DEFAULT_ROSTER)


def test_resolve_spells_paths_the_way_the_document_does():
    """`OsConfig.default_model` is the FIELD name; `os.defaults.model` is the path the
    user types at `jarvis config set`, and the resolved map owes them the latter."""
    resolved = cv.resolve(catalog.parse_catalog({}))

    assert "os.defaults.model" in resolved
    assert "os.default_model" not in resolved
    assert "os.notifications.telegram.token_env" in resolved
    assert "os.ui.port" in resolved


def test_resolve_covers_projects_under_their_own_prefix(tmp_path):
    (tmp_path / "p").mkdir()
    cat = catalog.parse_catalog({"projects": [{
        "name": "proj_a",
        "path": str(tmp_path / "p"),
        "worker": {"permission_mode": "plan"},
        "gates": {"enabled": ["pr_merge"]},
    }]})
    resolved = cv.resolve(cat)

    assert resolved["projects.proj_a.worker.permission_mode"] == "plan"
    assert resolved["projects.proj_a.gates.enabled"] == ["pr_merge"]
    assert resolved["projects.proj_a.max_concurrent"] == catalog.DEFAULT_MAX_CONCURRENT
    # `raw` is the user's document, stored whole as document_json; `name` is the segment
    # these hang under. Neither is a setting.
    assert not [k for k in resolved if k.endswith(".raw") or k.endswith(".name")]
    assert "projects.proj_a.gates.enabled" in resolved


def test_a_resolved_map_survives_a_json_round_trip(tmp_path):
    """It is stored as `resolved_json`, so a Path or a frozenset reaching it unconverted
    is a write that raises months later, on the one machine that has a gates block."""
    (tmp_path / "p").mkdir()
    cat = catalog.parse_catalog({"projects": [{
        "name": "proj_a", "path": str(tmp_path / "p"),
        "gates": {"enabled": ["pr_merge"], "patterns": {"pr_merge": ["^gh pr merge"]}},
    }]})
    resolved = cv.resolve(cat)

    assert json.loads(json.dumps(resolved)) == resolved
    assert resolved["projects.proj_a.path"] == str(tmp_path / "p")


def test_every_safety_glob_matches_a_path_that_actually_resolves(tmp_path):
    """A glob spelt against paths that do not exist is a safety rail that never fires,
    and nothing else in the suite would notice. §7."""
    (tmp_path / "p").mkdir()
    resolved = cv.resolve(catalog.parse_catalog({"projects": [
        {"name": "proj_a", "path": str(tmp_path / "p"),
         "gates": {"enabled": ["pr_merge"]}},
    ]}))

    for glob in catalog.SAFETY_KEYS:
        assert [p for p in resolved if fnmatch.fnmatch(p, glob)], glob
    # ... paired with the money settings, which must NOT match any of them
    for path in ("os.defaults.model", "os.defaults.max_concurrent",
                 "projects.proj_a.worker.autocompact_window"):
        assert not [g for g in catalog.SAFETY_KEYS if fnmatch.fnmatch(path, g)], path


# --- diff ---------------------------------------------------------------------------

def test_diff_names_the_kind_because_a_real_null_looks_like_an_absent_key():
    before = {"os.validation.enabled": False, "os.defaults.effort": None,
              "os.gone": 1}
    after = {"os.validation.enabled": True, "os.defaults.effort": None,
             "os.new": 2}

    assert cv.diff(before, after) == [
        {"path": "os.gone", "kind": "removed", "old": 1, "new": None},
        {"path": "os.new", "kind": "added", "old": None, "new": 2},
        {"path": "os.validation.enabled", "kind": "changed",
         "old": False, "new": True},
    ]
    # `os.defaults.effort` is None on BOTH sides and is not a change — the pairing that
    # a diff treating None as absent would fail.
    assert cv.diff(before, before) == []


# --- validation_config_from_resolved ------------------------------------------------

def test_validation_config_round_trips_through_the_resolved_map():
    """§6 forbids ever re-parsing a historical document with `parse_catalog`, so this is
    the only legal way to judge a round under the version it was stamped with."""
    cat = catalog.parse_catalog({"os": {"validation": {
        "max_rounds": 7,
        "roster": ["tester", "chair"],
        "seat_models": {"tester": "haiku"},
        "diff_chars": 123,
    }}})
    stored = json.loads(json.dumps(cv.resolve(cat)))  # as it comes back out of the DB

    assert cv.validation_config_from_resolved(stored) == cat.os.validation


def test_a_project_prefix_is_a_lookup_with_a_whole_block_fallback():
    """A LOOKUP, NOT A MERGE. `projects[].validation` does not exist in the catalog yet —
    the per-project work order adds it — so this builds the map by hand to pin the rule
    that survives its landing: if the project prefix carries validation keys they are
    used WHOLE, otherwise the `os` block is.
    """
    resolved = {
        "os.validation.enabled": False,
        "os.validation.max_rounds": 3,
        "projects.proj_a.validation.enabled": True,
        "projects.proj_a.validation.max_rounds": 9,
    }

    assert cv.validation_config_from_resolved(resolved, "proj_a").enabled is True
    assert cv.validation_config_from_resolved(resolved, "proj_a").max_rounds == 9
    # paired with the project that has no block of its own, and with the OS-level read
    assert cv.validation_config_from_resolved(resolved, "proj_b").enabled is False
    assert cv.validation_config_from_resolved(resolved).enabled is False


def test_the_fallback_is_the_only_behaviour_available_today():
    """Today `resolve()` yields no project validation paths at all, so every project
    falls back — and it must fall back to the STORED os block, not to bare defaults,
    which would silently judge a round under a configuration nobody chose."""
    cat = catalog.parse_catalog({"os": {"validation": {"enabled": True,
                                                       "max_rounds": 5}}})
    resolved = cv.resolve(cat)

    assert not [k for k in resolved if k.startswith("projects.")]
    assert cv.validation_config_from_resolved(resolved, "proj_a") == cat.os.validation
    assert cv.validation_config_from_resolved(resolved, "proj_a").max_rounds == 5


def test_a_map_written_before_a_field_existed_falls_back_to_that_fields_default():
    """A historical version is never migrated (§6). A key added by a later release is
    simply absent from an old row, and the shipped default has to stand for it."""
    cfg = cv.validation_config_from_resolved({"os.validation.enabled": True})

    assert cfg.enabled is True
    assert cfg.max_rounds == catalog.DEFAULT_VALIDATION_MAX_ROUNDS
    assert cfg.roster == catalog.DEFAULT_VALIDATION_ROSTER


# --- the store ----------------------------------------------------------------------

def test_adding_the_same_document_twice_writes_one_row(store):
    doc = {"os": {"validation": {"enabled": True}}}
    resolved = cv.resolve(catalog.parse_catalog(doc))

    first = store.add_config_version(doc, resolved, actor="user", reason="trying it")
    second = store.add_config_version({"os": {"validation": {"enabled": True}}},
                                      resolved, actor="file", reason="different words")

    assert second == first                      # the EXISTING row, not a new one
    assert second["actor"] == "user"            # ... so the second call wrote nothing
    assert len(store.config_versions()) == 1


def test_a_version_carries_its_document_verbatim_and_its_evidence_decoded(store):
    """`document_json` is the RAW document — an unknown key the user wrote deliberately
    must survive, which is why it is never `asdict(Catalog)`."""
    doc = {"//": "a pseudo-comment", "os": {"validation": {"enabled": True}},
           "invented_later": 1}
    resolved = cv.resolve(catalog.parse_catalog(doc))

    row = store.add_config_version(doc, resolved, actor="user", reason="why",
                                   changes=[{"path": "os.validation.enabled",
                                             "kind": "changed",
                                             "old": False, "new": True}],
                                   source_path="/tmp/catalog.json",
                                   schema_version="0.7.3")

    assert row["document"] == doc               # including the two keys the parser drops
    assert row["document_json"] == cv.canonicalise(doc)
    assert row["resolved"]["os.validation.enabled"] is True
    assert row["changes"][0]["path"] == "os.validation.enabled"
    assert row["schema_version"] == "0.7.3"
    assert row["source_path"] == "/tmp/catalog.json"
    assert row["id"] == cv.version_id(doc)


def test_the_head_is_the_newest_version_and_a_fresh_fleet_has_none(store):
    assert store.head_config_version() is None  # "before the console existed", not v1

    first = store.add_config_version({"os": {}}, {}, actor="user")
    second = store.add_config_version({"os": {"ui": {"port": 9000}}}, {}, actor="user")

    assert store.head_config_version()["id"] == second["id"]
    assert [v["id"] for v in store.config_versions()] == [second["id"], first["id"]]
    assert [v["id"] for v in store.config_versions(limit=1)] == [second["id"]]


def test_the_head_is_total_even_when_two_writes_share_a_timestamp(store):
    """`ts` is a float from `time.time()`; two writes inside one tick must still have a
    head, or `jarvis config history` disagrees with itself between calls."""
    first = store.add_config_version({"a": 1}, {}, actor="user")
    second = store.add_config_version({"a": 2}, {}, actor="user")
    store.conn.execute("UPDATE os_config_versions SET ts=1.0")

    assert store.head_config_version()["id"] == second["id"]
    assert [v["id"] for v in store.config_versions()] == [second["id"], first["id"]]


def test_an_unknown_version_id_is_none_not_an_error(store):
    assert store.get_config_version("cfg-0000000000000000") is None
