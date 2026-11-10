"""`jarvis do attach`: durable raw outputs (docs/superpowers/specs/2026-11-10-decision-order-experiments.md §6)"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest

from jarvis import cli, hooks, ops, project_store, specs
from jarvis.project_store import DECISION_ATTACHED_EVENT, ProjectStore

REPORT = {
    "summary": "B is 30% cheaper",
    "findings": [{"key": "f1", "title": "cost", "detail": "d", "evidence": ["x"]}],
    "recommendation": {"option": "", "why": "cheaper"},
    "proposed_orders": [],
}


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project, monkeypatch):
    monkeypatch.delenv("JARVIS_WO_ID", raising=False)
    ops.start_os(str(catalog_file), foreground=True)


@pytest.fixture()
def store(project):
    s = ProjectStore(project)
    yield s
    s.close()


def _do(store, *, phase: str | None = "running") -> tuple[str, str]:
    """A researching do whose open experiment is in `phase`, seeded through the §2 helpers"""
    do = ops.create_decision_order("proj_a", "which cache", "compare the two caches")["id"]
    wo = store.create_work_order("research it", kind="researcher", parent_id=do)["id"]
    store.set_status(wo, "running")
    store.update_feature_order(do, plan_wo_id=wo, plan=project_store.db.to_json(REPORT))
    store.set_feature_status(do, "planning")
    if phase:
        stamps = {"forecast_due": {}, "awaiting_go": {"forecast": {"runs": 1}},
                  "running": {"forecast": {"runs": 1}, "go_at": 2.0}}[phase]
        store.append_experiment(do, {
            "key": "p1", "title": "t", "brief": "b", "researcher_id": wo, "started_at": 1.0,
            "reopened_from": "awaiting_decision", "forecast": None, "go_at": None,
            "attachments": [], "reported_at": None, **stamps})
    return do, wo


def _file(tmp_path, name: str, body: bytes = b"row\n"):
    p = tmp_path / name
    p.write_bytes(body)
    return p


def _refusal_commands(message: str) -> list[str]:
    return [c.replace("<file>...", "x") for c in re.findall(r"`(jarvis [^`]+)`", message)]


def _accepted(command: str) -> None:
    """The refusal's named command parses (the convention: every refusal names a real fix)"""
    cli.build_parser().parse_args(command.split()[1:])


@pytest.mark.parametrize("phase", ["awaiting_go", "running"])
def test_copy_manifest_and_sha256(started, store, project, tmp_path, phase):
    do, wo = _do(store, phase=phase)
    src = _file(tmp_path, "out.jsonl", b'{"a": 1}\n')

    out = ops.attach_decision_outputs(do, [src])

    [got] = out["attached"]
    dest = specs.experiment_dir(project, do, 1, "p1") / "out.jsonl"
    assert got["path"] == str(dest) and dest.read_bytes() == src.read_bytes()
    sha = hashlib.sha256(src.read_bytes()).hexdigest()
    [mark] = store.experiments_of(do)[0]["attachments"]
    assert mark == {"name": "out.jsonl", "size": 9, "sha256": sha,
                    "path": str(dest.relative_to(project)), "at": mark["at"]}
    [event] = [e for e in store.list_events(wo) if e["kind"] == DECISION_ATTACHED_EVENT]
    assert json.loads(event["payload"]) == {"do_id": do, "n": 1, "name": "out.jsonl",
                                         "size": 9, "sha256": sha}


def test_a_symlink_is_resolved_and_its_target_copied(started, store, project, tmp_path):
    do, _ = _do(store)
    target = _file(tmp_path, "real.csv", b"a,b\n")
    link = tmp_path / "link.csv"
    link.symlink_to(target)

    [got] = ops.attach_decision_outputs(do, [link])["attached"]

    assert got["name"] == "real.csv"
    stored = specs.experiment_dir(project, do, 1, "p1") / "real.csv"
    assert not stored.is_symlink() and stored.read_bytes() == b"a,b\n"


def test_a_clash_gets_a_suffix_never_an_overwrite(started, store, project, tmp_path):
    do, _ = _do(store)
    (tmp_path / "a").mkdir()
    (tmp_path / "b").mkdir()
    a = _file(tmp_path / "a", "out.txt", b"1")
    b = _file(tmp_path / "b", "out.txt", b"2")
    ops.attach_decision_outputs(do, [a])

    names = [g["name"] for g in ops.attach_decision_outputs(do, [b, a])["attached"]]

    assert names == ["out-2.txt", "out-3.txt"]
    d = specs.experiment_dir(project, do, 1, "p1")
    assert (d / "out.txt").read_bytes() == b"1" and (d / "out-2.txt").read_bytes() == b"2"


@pytest.mark.parametrize("cap, n_held, size_held, body", [
    ("ATTACH_MAX_FILE_BYTES", 0, 0, b"x" * 11),
    ("ATTACH_MAX_EXPERIMENT_BYTES", 1, 95, b"x" * 6),
    ("ATTACH_MAX_FILES", 10, 0, b"x"),
])
def test_every_cap_refusal_files_nothing(started, store, project, tmp_path, monkeypatch,
                                         cap, n_held, size_held, body):
    monkeypatch.setattr(ops, cap, 10 if cap != "ATTACH_MAX_EXPERIMENT_BYTES" else 100)
    do, wo = _do(store)
    for i in range(n_held):
        store.add_experiment_attachment(do, 1, {"name": f"h{i}", "size": size_held,
                                                "sha256": "", "path": "", "at": 0.0})
    ok = _file(tmp_path, "ok.txt", b"x")
    big = _file(tmp_path, "big.txt", body)

    with pytest.raises(ops.OpsError, match="cap") as e:
        ops.attach_decision_outputs(do, [ok, big])

    assert "Nothing was attached" in str(e.value)
    for c in _refusal_commands(str(e.value)):
        _accepted(c)
    assert len(store.experiments_of(do)[0]["attachments"]) == n_held
    assert not specs.experiment_dir(project, do, 1, "p1").exists()
    assert not [e for e in store.list_events(wo) if e["kind"] == DECISION_ATTACHED_EVENT]


def test_a_directory_or_missing_path_is_refused_and_files_nothing(started, store, project,
                                                                  tmp_path):
    do, _ = _do(store)
    ok = _file(tmp_path, "ok.txt")
    (tmp_path / "outs").mkdir()
    with pytest.raises(ops.OpsError, match="pass the files, or tar them first") as e:
        ops.attach_decision_outputs(do, [ok, tmp_path / "outs"])
    for c in _refusal_commands(str(e.value)):
        _accepted(c)
    with pytest.raises(ops.OpsError, match="not an existing regular file"):
        ops.attach_decision_outputs(do, [ok, tmp_path / "gone.txt"])
    assert not specs.experiment_dir(project, do, 1, "p1").exists()


@pytest.mark.parametrize("phase", [None, "forecast_due"])
def test_refused_with_no_open_experiment(started, store, tmp_path, phase):
    do, _ = _do(store, phase=phase)
    with pytest.raises(ops.OpsError, match=f"jarvis do show {do}") as e:
        ops.attach_decision_outputs(do, [_file(tmp_path, "out.txt")])
    for c in _refusal_commands(str(e.value)):
        _accepted(c)
    # A reported experiment is not open either
    if phase is None:
        store.append_experiment(do, {"key": "", "forecast": {"runs": 1}, "go_at": 1.0,
                                     "reported_at": 2.0})
        with pytest.raises(ops.OpsError, match="no open experiment"):
            ops.attach_decision_outputs(do, [_file(tmp_path, "out.txt")])


def test_the_files_survive_do_done(started, store, project, tmp_path):
    do, _ = _do(store)
    [got] = ops.attach_decision_outputs(do, [_file(tmp_path, "out.txt")])["attached"]
    store.set_feature_status(do, "awaiting_decision")

    ops.complete_decision_order(do, note="B")

    assert store.get_feature_order(do)["status"] == "completed"
    assert Path(got["path"]).read_bytes() == b"row\n"


def test_the_cli_is_a_thin_wrapper_and_the_researcher_may_run_it(started, store, tmp_path,
                                                                  capsys, monkeypatch):
    do, wo = _do(store)
    src = _file(tmp_path, "out.txt")
    # A researcher verb: it runs inside the researcher's own session
    monkeypatch.setenv("JARVIS_WO_ID", wo)
    assert cli.main(["do", "attach", do, str(src), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["attached"][0]["name"] == "out.txt"
    env = {"JARVIS_WO_ID": wo, hooks.WO_KIND_ENV: "researcher"}
    payload = {"tool_name": "Bash", "cwd": "/tmp",
               "tool_input": {"command": f"jarvis do attach {do} /tmp/out.jsonl"}}
    assert hooks.researcher_bash_decision(payload, env) is None
