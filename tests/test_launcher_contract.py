"""The launcher contract: validation, templating, resolution and drift.

These are the unit-level guarantees. `test_launcher_pipeline.py` drives the same
contract through a real dispatch.
"""

from __future__ import annotations

import json

import pytest

from jarvis import launcher as lm
from jarvis.catalog import ProjectSpec


def minimal() -> dict:
    return {
        "schema_version": 1,
        "name": "wrap",
        "spawn": {"command": ["wrap", "run", "--", "{prompt}"]},
        "list": {"command": ["wrap", "ps"]},
    }


# -- validation ---------------------------------------------------------------------


def test_minimal_contract_validates():
    assert lm.validate_contract(minimal()) == []


def test_required_pieces_are_named_individually():
    problems = lm.validate_contract({"schema_version": 1})
    assert any("name" in p for p in problems)
    assert any("spawn" in p for p in problems)
    assert any("list" in p for p in problems)


def test_unknown_placeholder_is_rejected():
    c = minimal()
    c["spawn"]["command"] = ["wrap", "run", "{promt}"]
    problems = lm.validate_contract(c)
    assert any("{promt}" in p for p in problems)


def test_placeholder_valid_for_one_verb_is_invalid_for_another():
    c = minimal()
    c["stop"] = {"command": ["wrap", "kill", "{session_id}"]}
    problems = lm.validate_contract(c)
    assert any("stop" in p and "session_id" in p for p in problems)


def test_state_map_may_not_produce_the_word_running():
    """`running` is a work-order status, not a session state — the confusion that
    once made every healthy worker read as blocked."""
    c = minimal()
    c["list"]["state_map"] = {"GOING": "running"}
    problems = lm.validate_contract(c)
    assert any("running" in p for p in problems)


def test_load_contract_reports_every_problem_at_once(tmp_path):
    path = tmp_path / "launcher.json"
    path.write_text(json.dumps({"schema_version": 99, "list": {}}))
    with pytest.raises(lm.LauncherError) as e:
        lm.load_contract(path)
    assert "schema_version" in str(e.value) and "name" in str(e.value)


# -- templating ---------------------------------------------------------------------


def test_conditional_group_is_dropped_when_its_variable_is_empty():
    command = ["wrap", {"if": "model", "args": ["--model", "{model}"]}, "{prompt}"]
    assert lm.render_command(command, {"model": None, "prompt": "go"}) == ["wrap", "go"]
    assert lm.render_command(command, {"model": "opus", "prompt": "go"}) == [
        "wrap", "--model", "opus", "go"]


def test_list_variable_repeats_its_group_per_element():
    command = [{"if": "add_dirs", "args": ["--dir", "{item}"]}]
    assert lm.render_command(command, {"add_dirs": ["/a", "/b"]}) == [
        "--dir", "/a", "--dir", "/b"]


def test_lone_empty_placeholder_disappears_instead_of_becoming_an_empty_arg():
    assert lm.render_command(["wrap", "{model}", "{prompt}"],
                             {"model": "", "prompt": "go"}) == ["wrap", "go"]


def test_prompt_is_passed_whole_including_newlines_and_dashes():
    prompt = "--not-a-flag\nsecond line\twith tabs"
    assert lm.render_command(["wrap", "--", "{prompt}"], {"prompt": prompt})[-1] == prompt


# -- extraction ---------------------------------------------------------------------


def test_extract_regex_takes_the_first_capture_group():
    assert lm.extract({"from": "stdout", "regex": r"job:(\w+)"}, "started job:ab12\n") == "ab12"


def test_extract_json_walks_a_dotted_path():
    payload = json.dumps({"out": {"jobs": [{"id": "x"}]}})
    assert lm.extract({"from": "stdout_json", "path": "out.jobs.0.id"}, payload) == "x"


def test_extract_json_on_garbage_says_so():
    with pytest.raises(lm.LauncherError):
        lm.extract({"from": "stdout_json"}, "not json")


# -- resolution ---------------------------------------------------------------------


def test_no_contract_anywhere_means_the_native_launcher(tmp_path, jarvis_home):
    spec = ProjectSpec(name="p", path=tmp_path)
    launcher = lm.launcher_for(spec)
    assert isinstance(launcher, lm.NativeLauncher)
    assert launcher.capabilities.worktree is True


def test_project_contract_beats_the_fleet_default(tmp_path, jarvis_home):
    jarvis_home.mkdir(parents=True, exist_ok=True)
    fleet = dict(minimal(), name="fleet-wide")
    (jarvis_home / "launcher.json").write_text(json.dumps(fleet))
    project = tmp_path / "proj"
    (project / ".jarvis").mkdir(parents=True)
    (project / ".jarvis" / "launcher.json").write_text(
        json.dumps(dict(minimal(), name="project-specific")))

    assert lm.launcher_for(ProjectSpec(name="p", path=project)).name == "project-specific"
    assert lm.launcher_for(ProjectSpec(name="q", path=tmp_path / "other")).name == "fleet-wide"


def test_catalog_override_beats_the_project_contract(tmp_path, jarvis_home):
    project = tmp_path / "proj"
    (project / ".jarvis").mkdir(parents=True)
    (project / ".jarvis" / "launcher.json").write_text(
        json.dumps(dict(minimal(), name="on-disk")))
    override = tmp_path / "elsewhere.json"
    override.write_text(json.dumps(dict(minimal(), name="from-catalog")))

    spec = ProjectSpec(name="p", path=project, launcher=str(override))
    assert lm.launcher_for(spec).name == "from-catalog"


def test_catalog_pointer_at_a_missing_file_is_an_error_not_a_fallback(tmp_path, jarvis_home):
    spec = ProjectSpec(name="p", path=tmp_path, launcher=str(tmp_path / "nope.json"))
    with pytest.raises(lm.LauncherError):
        lm.launcher_for(spec)


# -- provenance / drift ---------------------------------------------------------------


def test_fingerprint_ignores_provenance_but_tracks_behaviour():
    a = minimal()
    b = dict(minimal(), provenance={"notes": "written on a tuesday"})
    assert lm.fingerprint(a) == lm.fingerprint(b)
    c = minimal()
    c["spawn"]["command"] = ["wrap", "run", "--fast", "--", "{prompt}"]
    assert lm.fingerprint(c) != lm.fingerprint(a)


def test_source_drift_notices_a_changed_wrapper(tmp_path):
    wrapper = tmp_path / "bgwrap"
    wrapper.write_text("v1")
    contract = dict(minimal(),
                    provenance={"sources": [{"path": str(wrapper), "sha256": "auto"}]})
    lm.stamp_source_digests(contract)
    assert lm.source_drift(contract) == []

    wrapper.write_text("v2 — the user changed their wrapper")
    assert lm.source_drift(contract) == [str(wrapper)]

    wrapper.unlink()
    assert lm.source_drift(contract) == [f"{wrapper} (gone)"]


# -- the contract launcher against a real wrapper --------------------------------------


def test_contract_launcher_drives_the_wrapper(tmp_path, fake_wrapper):
    launcher = lm.ContractLauncher(fake_wrapper.contract, source="test")
    job_id = launcher.spawn(prompt="do the thing", cwd=tmp_path, name="[WO wo-1] t",
                            model="opus")
    assert job_id and fake_wrapper.jobs[0]["job"] == job_id

    sessions = launcher.roster(tmp_path)
    assert len(sessions) == 1
    # The wrapper's own vocabulary has been translated into Jarvis's.
    assert sessions[0].state == "working" and sessions[0].is_active
    assert sessions[0].session_id == "conv-" + job_id

    assert launcher.result(job_id) == ("working", "")
    fake_wrapper.finish(job_id, "final: it is done")
    state, text = launcher.result(job_id)
    assert state == "done" and text == "final: it is done"

    assert launcher.send("conv-x", "more context", cwd=tmp_path) == "ack: more context"
    assert launcher.stop(job_id) is True
    assert fake_wrapper.jobs == []


def test_a_failing_wrapper_surfaces_as_a_launcher_error(tmp_path):
    contract = dict(minimal(), name="broken")
    contract["spawn"]["command"] = ["definitely-not-a-real-binary-xyz", "{prompt}"]
    launcher = lm.ContractLauncher(contract, source="test")
    assert launcher.available() is False
    with pytest.raises(lm.LauncherError) as e:
        launcher.spawn(prompt="go", cwd=tmp_path, name="n")
    assert "not found" in str(e.value)


def test_verify_static_flags_a_missing_binary(tmp_path, jarvis_home):
    contract = dict(minimal(), name="broken")
    contract["spawn"]["command"] = ["definitely-not-a-real-binary-xyz", "{prompt}"]
    report = lm.verify(lm.ContractLauncher(contract, "test"), tmp_path, live=False)
    assert report["ok"] is False
    assert any(c["check"] == "binary" and not c["ok"] for c in report["checks"])


def test_verify_live_spawns_a_probe_and_always_stops_it(tmp_path, fake_wrapper):
    launcher = lm.ContractLauncher(fake_wrapper.contract, source="test")
    report = lm.verify(launcher, tmp_path, live=True, timeout=5)
    assert report["ok"] is True, report["checks"]
    assert {c["check"] for c in report["checks"]} >= {"spawn", "list", "stop"}
    # No probe session is left behind for the user to find and wonder about.
    assert fake_wrapper.jobs == []
