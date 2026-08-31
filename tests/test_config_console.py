"""The config console's write path: `jarvis config` and the `ops` functions behind it.

docs/superpowers/specs/2026-08-27-the-config-console.md §3, §7, §8. The ledger's own
arithmetic (canonical form, ids, `resolve`) is proved in `tests/test_config_version.py`;
what is proved here is the WRITE — its order, what it refuses, and who may perform it.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from jarvis import cli, config_version as cv, gate_rules, ops
from jarvis.central_store import CentralStore

DOCUMENT = {
    "os": {"defaults": {"model": "opus"}},
    "projects": [
        {"name": "proj_a", "path": "/tmp", "description": "one"},
        {"name": "proj_b", "path": "/tmp", "description": "two"},
    ],
}


@pytest.fixture(autouse=True)
def not_a_worker(monkeypatch):
    """The suite is routinely run BY a worker, which inherits `JARVIS_WO_ID` — and the
    lockout under test reads exactly that. The isolation gate cannot scrub it globally:
    `testing._bills_real_tokens` needs it to attribute a real-model eval's spend.
    """
    monkeypatch.delenv("JARVIS_WO_ID", raising=False)


@pytest.fixture()
def catalog(tmp_path) -> Path:
    """A registered catalog, the way `jarvis start` leaves one."""
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps(DOCUMENT, indent=4))  # deliberately NOT canonical
    store = CentralStore()
    store.set_state("catalog_path", str(path))
    store.close()
    return path


def head() -> dict:
    store = CentralStore()
    try:
        row = store.head_config_version()
    finally:
        store.close()
    assert row is not None
    return row


def no_head() -> bool:
    store = CentralStore()
    try:
        return store.head_config_version() is None
    finally:
        store.close()


def document_of(path: Path) -> dict:
    return json.loads(path.read_text())


# --- the write path: validate, rewrite the file, record the row ----------------------

def test_a_set_rewrites_the_file_canonically_and_records_the_version(catalog):
    res = ops.set_config("os.ui.port", 9001)

    assert document_of(catalog)["os"]["ui"]["port"] == 9001
    # The file is rewritten FROM the canonical document, which is what makes the stored
    # id address the bytes on disk — a hand-indented catalog is reflowed (§3).
    assert catalog.read_text() == cv.canonicalise(document_of(catalog)) + "\n"
    assert head()["id"] == res["version"]["id"] == cv.version_id(document_of(catalog))
    assert res["version"]["actor"] == "user"
    assert res["change"] == {"path": "os.ui.port", "kind": "changed",
                             "old": 8787, "new": 9001}


def test_a_document_that_would_not_parse_is_refused_before_the_file_is_touched(catalog):
    """Validate → write → record, in that order (§3). Getting this backwards leaves a
    catalog the daemon cannot load, which is every project in the fleet, not one."""
    before = catalog.read_text()

    with pytest.raises(ops.OpsError, match="permission_mode"):
        ops.set_config("worker.permission_mode", "yolo", project="proj_a", reason="x")

    assert catalog.read_text() == before
    assert no_head()


def test_the_temp_file_is_a_sibling_of_the_catalog(catalog, monkeypatch):
    """`os.replace` is atomic within one filesystem and RAISES across two, so a temp
    file under /tmp turns the rename into a failure on any machine whose $JARVIS_HOME
    is a separate mount."""
    seen: list[tuple[Path, Path]] = []
    real = os.replace

    def spy(src, dst):
        seen.append((Path(src), Path(dst)))
        return real(src, dst)

    monkeypatch.setattr(ops.os, "replace", spy)
    ops.set_config("os.ui.port", 9002)

    (src, dst) = seen[0]
    assert src.parent == dst.parent == catalog.parent


def test_an_edit_that_changes_nothing_writes_no_new_version(catalog):
    first = ops.set_config("os.ui.port", 9001)
    again = ops.set_config("os.ui.port", 9001)

    assert again["version"]["id"] == first["version"]["id"]
    assert again["changed"] is False
    store = CentralStore()
    assert len(store.config_versions()) == 1
    store.close()


# --- the key space: the project positional SCOPES the path (Neo, q175) ---------------

def test_the_project_positional_scopes_the_path(catalog):
    """§10.3's acceptance sentence. The path is relative to the project, and it lands
    in that project's entry — the key space is flat, the document is not."""
    res = ops.set_config("validation.enabled", True, project="proj_a", reason="trying it")

    assert res["path"] == "projects.proj_a.validation.enabled"
    entry = next(p for p in document_of(catalog)["projects"] if p["name"] == "proj_a")
    assert entry["validation"] == {"enabled": True}
    assert "validation" not in document_of(catalog)["os"]


def test_an_absolute_path_is_accepted_beside_the_project_it_names(catalog):
    res = ops.set_config("projects.proj_a.max_concurrent", 4, project="proj_a")
    assert res["path"] == "projects.proj_a.max_concurrent"


@pytest.mark.parametrize("path", ["os.ui.port", "projects.proj_b.max_concurrent"])
def test_a_path_that_disagrees_with_the_project_positional_is_refused(catalog, path):
    """The half that matters: without it, `set proj_a projects.proj_b.…` silently edits
    a project the user did not name."""
    with pytest.raises(ops.OpsError, match="not a path under project 'proj_a'"):
        ops.set_config(path, 1, project="proj_a", reason="x")
    assert no_head()


def test_a_bare_path_without_a_project_names_neither_scope(catalog):
    with pytest.raises(ops.OpsError, match="not a setting path"):
        ops.set_config("validation.enabled", True, reason="x")


def test_an_unknown_project_is_named_with_the_ones_that_exist(catalog):
    with pytest.raises(ops.OpsError, match=r"unknown project 'nope'.*proj_a"):
        ops.set_config("max_concurrent", 2, project="nope")


# --- --reason, mandatory on the safety keys and only there ---------------------------

@pytest.mark.parametrize("project,path", [
    (None, "os.validation.enabled"),
    (None, "os.neo.enabled"),
    ("proj_a", "worker.permission_mode"),
    ("proj_a", "gates.enabled"),
])
def test_a_safety_setting_refuses_to_be_changed_without_a_reason(catalog, project, path):
    value = "acceptEdits" if path.endswith("permission_mode") else ["pr_merge"] \
        if path.endswith("gates.enabled") else True
    with pytest.raises(ops.OpsError, match="safety setting"):
        ops.set_config(path, value, project=project)
    assert no_head()

    res = ops.set_config(path, value, project=project, reason="because")
    assert res["safety"] is True
    assert res["version"]["reason"] == "because"


def test_a_money_setting_needs_no_reason(catalog):
    res = ops.set_config("os.defaults.max_concurrent", 4)
    assert res["safety"] is False
    assert res["version"]["reason"] == ""


def test_adopt_never_demands_a_reason_because_it_changes_nothing(catalog):
    """Every other write path refuses a safety change without one. `adopt` cannot: the
    edit already happened on disk and the fleet is already running it, so refusing would
    leave the record behind the file — the drift this command exists to close."""
    catalog.write_text(json.dumps({**DOCUMENT, "os": {"validation": {"enabled": True}}}))

    res = ops.adopt_config()
    assert res["adopted"] is True
    assert [c["path"] for c in res["changes"] if c["path"].endswith("validation.enabled")]


# --- the worker lockout (§7): the layer that actually stops one ----------------------

@pytest.mark.parametrize("call", [
    lambda: ops.set_config("os.ui.port", 9001),
    lambda: ops.unset_config("os.defaults.model"),
    lambda: ops.restore_config("cfg-0000000000000000"),
    lambda: ops.adopt_config(),
])
def test_a_worker_session_may_not_write_configuration(catalog, monkeypatch, call):
    """`ProjectSpec.gates` is empty by default and `hooks.preflight_decision` allows any
    `jarvis` command chain, so on an ungated project the `config_write` gate protects
    nobody. This refusal is what does."""
    monkeypatch.setenv("JARVIS_WO_ID", "wo-deadbeef")
    before = catalog.read_text()

    with pytest.raises(ops.OpsError, match="wo-deadbeef is a worker session"):
        call()

    assert catalog.read_text() == before
    assert no_head()


def test_reading_configuration_is_not_blocked_for_a_worker(catalog, monkeypatch):
    """The lockout is on the WRITE path only — a worker that cannot read what it is
    running under has been given a different problem."""
    monkeypatch.setenv("JARVIS_WO_ID", "wo-deadbeef")
    assert ops.config_get("os.defaults.model")["value"] == "opus"
    assert ops.config_show()["resolved"]["os.ui.port"] == 8787
    assert ops.config_history() == []


# --- the apply class the user is told about (§4.2) -----------------------------------

@pytest.mark.parametrize("path,cls", [
    ("os.validation.enabled", "hot"),
    ("os.neo.model", "hot"),               # hot despite being called `model`
    ("projects.p.max_concurrent", "hot"),
    ("os.defaults.model", "next-dispatch"),
    ("projects.p.worker.effort", "next-dispatch"),
    ("projects.p.worker.autocompact_window", "next-dispatch"),
    ("os.ui.port", "restart"),
    ("projects.p.path", "restart"),
    ("projects.p.settings_overrides.hooks", "restart"),
])
def test_every_class_in_the_design_table(path, cls):
    assert ops.apply_class(path) == cls


def test_settings_overrides_says_why_it_is_inert_rather_than_accepting_it_silently(
        catalog):
    res = ops.set_config("settings_overrides", {"env": {"X": "1"}}, project="proj_a")

    assert res["apply"] == "restart"
    assert "bootstrap_project" in res["note"] and "jarvis start" in res["note"]


# --- unset ---------------------------------------------------------------------------

def test_unset_drops_the_key_and_reports_the_default_it_falls_back_to(catalog):
    ops.set_config("os.defaults.model", "sonnet")
    res = ops.unset_config("os.defaults.model")

    assert "model" not in document_of(catalog)["os"]["defaults"]
    assert res["value"] == "claude-opus-5"
    assert res["change"] == {"path": "os.defaults.model", "kind": "changed",
                             "old": "sonnet", "new": "claude-opus-5"}


def test_unsetting_a_key_the_file_never_set_says_so(catalog):
    with pytest.raises(ops.OpsError, match="already running on its default"):
        ops.unset_config("os.ui.port")


# --- get and show --------------------------------------------------------------------

def test_get_distinguishes_a_written_setting_from_a_default(catalog):
    assert ops.config_get("os.defaults.model")["written"] is True
    port = ops.config_get("os.ui.port")
    assert (port["value"], port["written"], port["apply"]) == (8787, False, "restart")


def test_show_scopes_to_one_project_plus_the_fleet_settings_it_runs_under(catalog):
    resolved = ops.config_show(project="proj_a")["resolved"]

    assert "projects.proj_a.max_concurrent" in resolved
    assert "os.defaults.model" in resolved
    assert not [k for k in resolved if k.startswith("projects.proj_b.")]


def test_show_says_which_paths_the_document_sets_and_which_are_defaults(catalog):
    """`config_get`'s "set in the catalog" answer, for every key at once — the half of
    provenance the /config page cannot get from the ledger (§8, Neo q180)."""
    written = ops.config_show()["written"]

    assert "os.defaults.model" in written
    assert "os.ui.port" not in written  # a default of this build, not a choice


def test_show_of_a_version_reads_its_own_document_not_the_file(catalog):
    """A stored version's provenance is the document that was stored with it: the file
    has moved on, and reading it here would date-stamp an old version with new facts."""
    row = ops.set_config("os.ui.port", 9999)["version"]
    ops.unset_config("os.ui.port")

    assert "os.ui.port" not in ops.config_show()["written"]
    assert "os.ui.port" in ops.config_show(version=row["id"])["written"]


def test_show_flags_a_file_the_ledger_has_never_seen(catalog):
    """The drift the invariant will report, visible in the command that reads config."""
    before = ops.config_show()
    assert before["drift"] is True and before["version"] is None

    ops.adopt_config()
    assert ops.config_show()["drift"] is False


# --- adopt ---------------------------------------------------------------------------

def test_adopt_records_a_hand_edited_file_once(catalog):
    first = ops.adopt_config()
    assert first["adopted"] is True and first["version"]["actor"] == "file"

    again = ops.adopt_config()
    assert again["adopted"] is False
    assert again["version"]["id"] == first["version"]["id"]
    store = CentralStore()
    assert len(store.config_versions()) == 1
    store.close()


def test_adopt_records_what_the_hand_edit_changed(catalog):
    ops.set_config("os.ui.port", 9001)
    catalog.write_text(json.dumps({**DOCUMENT, "os": {"defaults": {"model": "sonnet"}}}))

    res = ops.adopt_config(reason="edited by hand")
    paths = {c["path"]: c for c in res["changes"]}

    assert paths["os.ui.port"]["old"] == 9001 and paths["os.ui.port"]["new"] == 8787
    assert paths["os.defaults.model"]["new"] == "sonnet"


# --- history and diff ----------------------------------------------------------------

def test_history_is_newest_first_and_marks_the_applied_version(catalog):
    ops.set_config("os.ui.port", 9001)
    second = ops.set_config("os.defaults.model", "sonnet")

    rows = ops.config_history()
    assert [r["id"] for r in rows][0] == second["version"]["id"]
    assert [r["head"] for r in rows] == [True, False]


def test_a_project_filter_keeps_fleet_wide_versions_and_drops_other_projects(catalog):
    """An `os.` change reaches every project's effective config — project settings
    resolve against `os.defaults` at parse time — so the filter only drops versions that
    touch nothing but somebody else."""
    fleet = ops.set_config("os.defaults.model", "sonnet")
    mine = ops.set_config("max_concurrent", 3, project="proj_a")
    theirs = ops.set_config("max_concurrent", 7, project="proj_b")

    ids = [r["id"] for r in ops.config_history(project="proj_a")]
    assert mine["version"]["id"] in ids and fleet["version"]["id"] in ids
    assert theirs["version"]["id"] not in ids


def test_diff_reads_the_resolved_maps_and_takes_an_id_prefix(catalog):
    a = ops.set_config("os.ui.port", 9001)["version"]["id"]
    b = ops.set_config("os.ui.port", 9002)["version"]["id"]

    res = ops.config_diff(a[:10], b[:10])
    assert res["changes"] == [{"path": "os.ui.port", "kind": "changed",
                               "old": 9001, "new": 9002}]


def test_an_ambiguous_or_unknown_version_id_says_which(catalog):
    with pytest.raises(ops.OpsError, match="no config version"):
        ops.config_diff("cfg-nope", "cfg-nope")


# --- restore -------------------------------------------------------------------------

def test_restore_puts_the_document_back_and_makes_it_the_head(catalog):
    """The property that makes `restore` a REMEDY for drift rather than a cause of it
    (§3): ids are content-addressed, so restoring writes no row — the head must move to
    the restored id, or the record permanently names a document nobody is running."""
    first = ops.set_config("os.ui.port", 9001)["version"]["id"]
    ops.set_config("os.ui.port", 9002)

    res = ops.restore_config(first, reason="9002 was wrong")

    assert document_of(catalog)["os"]["ui"]["port"] == 9001
    assert res["version"]["id"] == first
    assert head()["id"] == first
    assert ops.config_show()["drift"] is False
    store = CentralStore()
    assert len(store.config_versions()) == 2  # no third row: restoring is not an edit
    store.close()


def test_restore_needs_a_reason_when_it_moves_a_safety_setting(catalog):
    safe = ops.set_config("os.ui.port", 9001)["version"]["id"]
    ops.set_config("os.validation.enabled", True, reason="trying it")

    with pytest.raises(ops.OpsError, match="safety setting"):
        ops.restore_config(safe)

    res = ops.restore_config(safe, reason="off again")
    assert res["classes"] == ["hot"]


# --- values --------------------------------------------------------------------------

@pytest.mark.parametrize("text,value", [
    ("true", True), ("false", False), ("3", 3), ("null", None),
    ('["a"]', ["a"]), ("opus", "opus"), ('"opus"', "opus"),
])
def test_a_value_is_json_when_it_parses_as_json_and_a_string_otherwise(text, value):
    assert ops.parse_config_value(text) == value


# --- the gate: a worker's attempt is recognised, and can never be exempted -----------

def test_every_config_write_verb_is_recognised_as_a_privileged_action():
    ruleset = gate_rules.RuleSet.from_seeds()
    kinds = gate_rules.KIND_NAMES
    for verb in ("set", "unset", "restore", "adopt"):
        match = ruleset.decide(f"jarvis config {verb} os.validation.enabled false",
                               kinds).match
        assert match is not None and match.kind == "config_write", verb
    # The read commands must NOT gate: a recogniser broad enough to catch them spends a
    # Neo review every time a worker looks at what it is running under.
    assert ruleset.decide("jarvis config show proj_a", kinds).match is None
    assert ruleset.decide("jarvis config history --json", kinds).match is None


def test_the_config_write_canaries_can_never_be_learned_away():
    """The recursion the canary cuts (§7): the console can turn gates off, so the
    command that turns them off must never acquire a learned exemption."""
    ruleset = gate_rules.RuleSet.from_seeds()
    assert ruleset.check_canaries() == []
    assert [c for k, c in gate_rules.SEED_CANARIES if k == "config_write"]


def test_the_new_builtin_reaches_a_database_seeded_by_an_earlier_release():
    """`_seed_gate_rules` returns early on a matching version key, so a kind added
    without bumping `SEED_VERSION` never reaches a live os.db (§7)."""
    assert gate_rules.SEED_VERSION != "1"

    store = CentralStore()
    store.set_state("gate_rules_seed", "1")
    store.conn.execute("DELETE FROM gate_rules WHERE kind='config_write'")
    store.close()

    store = CentralStore()  # a reopen is the upgrade
    try:
        kinds = {r.kind for r in gate_rules.RuleSet.load(store).rules}
    finally:
        store.close()
    assert "config_write" in kinds


# --- the CLI -------------------------------------------------------------------------

def test_the_acceptance_sentence_runs_end_to_end(catalog, capsys):
    """§10.3, minus the daemon half the reload work order owns: the sentence the user
    actually asked for, typed the way they would type it."""
    rc = cli.main(["config", "set", "proj_a", "validation.enabled", "true",
                   "--reason", "trying it"])
    out = capsys.readouterr().out

    assert rc == 0
    # `false → true` rather than `+ … = true`: the old value is read off the RESOLVED
    # map, which yields `projects.<name>.validation.*` now that `ProjectSpec.validation`
    # exists — `resolve()` walks the dataclasses, so it needed no edit here (kn-fdd57936).
    assert "~ projects.proj_a.validation.enabled: false → true" in out
    assert "SAFETY SETTING" in out
    assert "hot — in force on the daemon's next tick" in out
    assert head()["reason"] == "trying it" and head()["actor"] == "user"


def test_the_cli_reports_a_refusal_rather_than_a_traceback(catalog, capsys):
    rc = cli.main(["config", "set", "proj_a", "validation.enabled", "true"])
    assert rc == 1
    assert "safety setting" in capsys.readouterr().err


def test_the_cli_renders_history_and_diff(catalog, capsys):
    a = ops.set_config("os.ui.port", 9001)["version"]["id"]
    b = ops.set_config("os.ui.port", 9002)["version"]["id"]

    cli.main(["config", "history"])
    out = capsys.readouterr().out
    assert out.startswith("● " + b)
    assert "~ os.ui.port: 9001 → 9002" in out

    cli.main(["config", "diff", a, b])
    assert "~ os.ui.port: 9001 → 9002" in capsys.readouterr().out


def test_config_json_is_machine_readable_everywhere(catalog, capsys):
    cli.main(["config", "set", "os.ui.port", "9001", "--json"])
    assert json.loads(capsys.readouterr().out)["path"] == "os.ui.port"

    cli.main(["config", "show", "--json"])
    assert json.loads(capsys.readouterr().out)["resolved"]["os.ui.port"] == 9001


def test_an_unregistered_catalog_says_how_to_name_one(tmp_path, capsys):
    rc = cli.main(["config", "get", "os.ui.port"])
    assert rc == 1
    assert "jarvis start --catalog" in capsys.readouterr().err

    path = tmp_path / "other.json"
    path.write_text(json.dumps(DOCUMENT))
    cli.main(["config", "get", "os.defaults.model", "--catalog", str(path)])
    assert '"opus"' in capsys.readouterr().out
