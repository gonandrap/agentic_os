"""Session dispatch: a work order's `claude` launch goes through its project's local launcher"""

import json
import os
import stat
import time
from pathlib import Path

import pytest

from jarvis import claude_cli, session_dispatch
from jarvis.catalog import CatalogError, load_catalog

LAUNCHER = """#!/usr/bin/env bash
set -euo pipefail
python3 - "$@" <<'PY'
import json, os, sys, time
rec = {"argv": sys.argv[1:], "cwd": os.getcwd(),
       "env": {k: v for k, v in os.environ.items()
               if k.startswith("JARVIS_LAUNCH_") or k in ("JARVIS_REAL_CLAUDE", "ASE_REPO")}}
with open(os.path.join(os.environ["LAUNCH_LOG_DIR"], f"{time.time_ns()}.json"), "w") as f:
    json.dump(rec, f)
PY
exec "$JARVIS_REAL_CLAUDE" "$@"
"""


@pytest.fixture()
def launcher(tmp_path, monkeypatch):
    logdir = tmp_path / "launch-log"
    logdir.mkdir()
    script = tmp_path / "launchers" / "ase.sh"
    script.parent.mkdir()
    script.write_text(LAUNCHER)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("LAUNCH_LOG_DIR", str(logdir))

    class Handle:
        path = script

        @property
        def calls(self) -> list[dict]:
            return [json.loads(p.read_text()) for p in sorted(logdir.iterdir())]

    return Handle()


def _wait_idle(session_id: str, timeout: float = 10.0) -> None:
    # clock-exempt: class 3 — spin-waits on the fake CLI's real background subprocess
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        entry = claude_cli.bg_roster().get(session_id)
        if entry is not None and entry.idle:
            return
        time.sleep(0.02)
    raise AssertionError(f"{session_id} never went idle")


# -- parsing the catalog's `worker.launcher`


def test_a_string_launcher_is_one_command(tmp_path):
    got = session_dispatch.parse_launcher("~/bin/claude-x", "where")
    assert got == session_dispatch.Launcher(command=(str(Path("~/bin/claude-x").expanduser()),))


def test_a_list_launcher_keeps_its_arguments():
    got = session_dispatch.parse_launcher(["/opt/ase.sh", "--quiet"], "where")
    assert got.command == ("/opt/ase.sh", "--quiet")
    assert got.env == {}


def test_an_object_launcher_carries_env():
    got = session_dispatch.parse_launcher(
        {"command": ["/opt/ase.sh"], "env": {"ASE_REPO": "agentic-env"}}, "where")
    assert got.command == ("/opt/ase.sh",)
    assert got.env == {"ASE_REPO": "agentic-env"}


def test_no_launcher_is_none():
    assert session_dispatch.parse_launcher(None, "where") is None


@pytest.mark.parametrize("raw", ["", [], {"env": {}}, {"command": 3}, 7,
                                 {"command": "x", "env": {"A": 1}},
                                 {"command": "x", "bogus": 1}])
def test_a_malformed_launcher_is_refused(raw):
    with pytest.raises(ValueError, match="where"):
        session_dispatch.parse_launcher(raw, "where")


def test_the_catalog_maps_a_project_to_its_launcher(tmp_path, launcher):
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"projects": [
        {"name": "ase", "path": str(tmp_path),
         "worker": {"launcher": {"command": str(launcher.path),
                                 "env": {"ASE_REPO": "agentic-env"}}}},
        {"name": "plain", "path": str(tmp_path)},
    ]}))
    cat = load_catalog(path)
    by = {p.name: p for p in cat.projects}
    assert by["ase"].worker.launcher.command == (str(launcher.path),)
    assert by["plain"].worker.launcher is None


def test_the_catalog_refuses_a_malformed_launcher(tmp_path):
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"projects": [
        {"name": "ase", "path": str(tmp_path), "worker": {"launcher": {"env": {}}}}]}))
    with pytest.raises(CatalogError, match="worker.launcher"):
        load_catalog(path)


# -- what one launch looks like


def test_a_launch_prefixes_the_claude_argv_and_tells_the_launcher_who_it_is(tmp_path):
    lch = session_dispatch.Launcher(command=("/opt/ase.sh", "--q"), env={"ASE_REPO": "x"})
    launch = session_dispatch.Launch(launcher=lch, project="ase",
                                     project_path=tmp_path, wo_id="wo-1",
                                     wo_kind="worker", phase="open",
                                     transport="background")
    assert launch.argv(["--bg", "--", "hi"]) == ["/opt/ase.sh", "--q", "--bg", "--", "hi"]
    env = launch.env()
    assert env["ASE_REPO"] == "x"
    assert env["JARVIS_REAL_CLAUDE"] == claude_cli.claude_bin()
    assert env["JARVIS_LAUNCH_PROJECT"] == "ase"
    assert env["JARVIS_LAUNCH_PROJECT_PATH"] == str(tmp_path)
    assert env["JARVIS_LAUNCH_WO_ID"] == "wo-1"
    assert env["JARVIS_LAUNCH_WO_KIND"] == "worker"
    assert env["JARVIS_LAUNCH_PHASE"] == "open"
    assert env["JARVIS_LAUNCH_TRANSPORT"] == "background"


def test_without_a_launcher_the_launch_is_bare_claude(tmp_path):
    launch = session_dispatch.Launch(launcher=None, project="p", project_path=tmp_path,
                                     wo_id="wo-1", wo_kind="worker", phase="open",
                                     transport="headless")
    assert launch.argv(["-p"]) == [claude_cli.claude_bin(), "-p"]
    assert launch.env() == {}


# -- the claude_cli primitives route launches, and only launches, through it


def _launch(launcher_path: Path, phase: str, tmp_path: Path,
            transport: str = "background") -> session_dispatch.Launch:
    return session_dispatch.Launch(
        launcher=session_dispatch.Launcher(command=(str(launcher_path),),
                                           env={"ASE_REPO": "agentic-env"}),
        project="ase", project_path=tmp_path, wo_id="wo-ase1", wo_kind="worker",
        phase=phase, transport=transport)


def test_a_background_open_goes_through_the_launcher(fake_claude, launcher, tmp_path):
    entry = claude_cli.spawn_bg_turn("go", tmp_path, "[WO wo-ase1] t",
                                     launch=_launch(launcher.path, "open", tmp_path))
    _wait_idle(entry.session_id)

    opens = [c for c in launcher.calls if "--bg" in c["argv"]]
    assert len(opens) == 1
    assert opens[0]["argv"][-2:] == ["--", "go"]
    assert opens[0]["env"]["JARVIS_LAUNCH_PHASE"] == "open"
    assert opens[0]["env"]["ASE_REPO"] == "agentic-env"
    # The launcher exec'd the real CLI with the argv untouched
    assert fake_claude.turns[entry.session_id] == ["go"]
    # Roster reads are control traffic, never launches
    assert not [c for c in launcher.calls if c["argv"][:1] == ["agents"]]


def test_a_background_resume_stays_bare_through_the_launcher(fake_claude, launcher, tmp_path):
    first = claude_cli.spawn_bg_turn("one", tmp_path, "n",
                                     launch=_launch(launcher.path, "open", tmp_path))
    _wait_idle(first.session_id)

    claude_cli.resume_bg_turn(first.session_id, "two", tmp_path,
                              launch=_launch(launcher.path, "resume", tmp_path))
    _wait_idle(first.session_id)

    resumes = [c for c in launcher.calls if "--resume" in c["argv"]]
    assert [c["argv"] for c in resumes] == [["--bg", "--resume", first.session_id, "--", "two"]]
    assert resumes[0]["env"]["JARVIS_LAUNCH_PHASE"] == "resume"
    assert fake_claude.turns[first.session_id] == ["one", "two"]
    assert not [c for c in launcher.calls if c["argv"][:1] == ["stop"]]


def test_a_headless_turn_goes_through_the_launcher(fake_claude, launcher, tmp_path):
    # clock-exempt: class 3 — spin-waits on a real headless turn subprocess dying
    out, err = tmp_path / "t" / "1.json", tmp_path / "t" / "1.err"
    spawned = claude_cli.spawn_turn("go", tmp_path, "11111111-2222-3333-4444-555555555555",
                                    out, err,
                                    launch=_launch(launcher.path, "open", tmp_path,
                                                   transport="headless"))
    deadline = time.monotonic() + 10
    while claude_cli.process_alive(spawned.pid) and time.monotonic() < deadline:
        time.sleep(0.02)
    assert claude_cli.read_turn_result(out, err) is not None
    [call] = launcher.calls
    assert "-p" in call["argv"] and call["argv"][-2:] == ["--", "go"]
    assert call["env"]["JARVIS_LAUNCH_TRANSPORT"] == "headless"


# -- end to end: a work order of a project with a launcher


from jarvis import ops, worker_session  # noqa: E402
from jarvis.project_store import ProjectStore  # noqa: E402


@pytest.fixture()
def launcher_fleet(jarvis_home, fake_claude, catalog_file, project, launcher, monkeypatch):
    def boot(transport: str):
        data = json.loads(catalog_file.read_text())
        data["os"]["defaults"]["transport"] = transport
        data["projects"][0].setdefault("worker", {})["launcher"] = {
            "command": str(launcher.path), "env": {"ASE_REPO": "agentic-env"}}
        catalog_file.write_text(json.dumps(data))
        monkeypatch.setattr(worker_session, "BG_DEAD_GRACE", 0.0)
        ops.start_os(str(catalog_file), foreground=True)
        catalog = load_catalog(catalog_file)
        return catalog.projects[0], ProjectStore(project)
    return boot


def test_every_turn_of_a_background_order_runs_through_its_launcher(
        launcher_fleet, launcher, fake_claude, settle_turns):
    project, store = launcher_fleet("background")
    wo = store.get_work_order(ops.create_work_order("proj_a", "task")["id"])

    worker_session.start(store, project, wo, "go")
    assert settle_turns(store)
    worker_session.send(store, project, store.get_work_order(wo["id"]), "again")
    assert settle_turns(store)

    launches = [c for c in launcher.calls if "--bg" in c["argv"]]
    assert [c["env"]["JARVIS_LAUNCH_PHASE"] for c in launches] == ["open", "resume"]
    assert {c["env"]["JARVIS_LAUNCH_WO_ID"] for c in launches} == {wo["id"]}
    assert {c["env"]["JARVIS_LAUNCH_PROJECT"] for c in launches} == {"proj_a"}
    sid = store.get_work_order(wo["id"])["session_id"]
    assert fake_claude.turns[sid] == ["go", "again"]


def test_a_headless_order_runs_through_its_launcher(launcher_fleet, launcher, settle_turns):
    project, store = launcher_fleet("headless")
    wo = store.get_work_order(ops.create_work_order("proj_a", "task")["id"])

    worker_session.start(store, project, wo, "go")
    assert settle_turns(store)

    [call] = [c for c in launcher.calls if "-p" in c["argv"]]
    assert call["env"]["JARVIS_LAUNCH_WO_ID"] == wo["id"]
    assert call["env"]["JARVIS_LAUNCH_TRANSPORT"] == "headless"
    assert store.latest_turn(wo["id"])["state"] == "done"


def test_a_project_without_a_launcher_launches_bare_claude(
        jarvis_home, fake_claude, catalog_file, project, launcher, settle_turns):
    ops.start_os(str(catalog_file), foreground=True)
    proj = load_catalog(catalog_file).projects[0]
    store = ProjectStore(project)
    wo = store.get_work_order(ops.create_work_order("proj_a", "task")["id"])
    worker_session.start(store, proj, wo, "go")
    assert settle_turns(store)
    assert launcher.calls == []
    assert os.environ.get("JARVIS_LAUNCH_PHASE") is None


def test_a_missing_launcher_is_named_in_the_error(fake_claude, tmp_path):
    gone = tmp_path / "nope" / "claude-ase.sh"
    with pytest.raises(claude_cli.ClaudeCliError, match="claude-ase.sh"):
        claude_cli.spawn_bg_turn("go", tmp_path, "n", launch=_launch(gone, "open", tmp_path))
    with pytest.raises(claude_cli.ClaudeCliError, match="claude-ase.sh"):
        claude_cli.spawn_turn("go", tmp_path, "11111111-2222-3333-4444-555555555555",
                              tmp_path / "t.json", tmp_path / "t.err",
                              launch=_launch(gone, "open", tmp_path, transport="headless"))


def _catalog_with(tmp_path, launcher_raw):
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"projects": [
        {"name": "ase", "path": str(tmp_path), "worker": {"launcher": launcher_raw}}]}))
    return load_catalog(path)


def test_doctor_flags_a_launcher_that_cannot_run(tmp_path):
    from jarvis import invariants

    plain = tmp_path / "not-executable.sh"
    plain.write_text("#!/bin/sh\n")
    for raw in (str(tmp_path / "missing.sh"), str(plain)):
        found = [v for v in invariants.check_catalog(_catalog_with(tmp_path, raw))
                 if v.invariant == invariants.INV_LAUNCHER_RUNNABLE]
        assert len(found) == 1 and found[0].context["project"] == "ase"


def test_doctor_accepts_a_runnable_launcher(tmp_path, launcher):
    from jarvis import invariants

    for raw in (str(launcher.path), "sh"):
        assert [v for v in invariants.check_catalog(_catalog_with(tmp_path, raw))
                if v.invariant == invariants.INV_LAUNCHER_RUNNABLE] == []


# -- template mode: a session manager (claude-jarvis start …) opens the session itself

OPEN = ["claude-jarvis", "start", "@jarvis", "--model", "{model}",
        "--compact-pct", "{compact_pct}", "--prompt", "{prompt}", "{wo_name}"]

WRAPPER = """#!/usr/bin/env bash
set -euo pipefail
python3 - "$@" <<'PY'
import json, os, sys, time
with open(os.path.join(os.environ["LAUNCH_LOG_DIR"], f"{time.time_ns()}.json"), "w") as f:
    json.dump({"argv": sys.argv[1:], "cwd": os.getcwd()}, f)
PY
[ "$1" = start ] || { echo "only start" >&2; exit 2; }
prompt="" model=""
while [ $# -gt 1 ]; do
  case "$1" in
    --prompt) prompt="$2"; shift 2 ;;
    --model) model="$2"; shift 2 ;;
    --compact-pct) shift 2 ;;
    *) shift ;;
  esac
done
name="$1"
wt="$PWD.worktrees/$name"
[ -e "$wt" ] && { echo "worktree $wt exists" >&2; exit 1; }
mkdir -p "$wt"
cd "$wt"
echo "preparing env $name" >&2
echo "HEAD is now at 0000000 init"
"$JARVIS_REAL_CLAUDE" --bg -n "$name" --model "$model" -- "$prompt"
"""


@pytest.fixture()
def wrapper(tmp_path, monkeypatch):
    logdir = tmp_path / "wrapper-log"
    logdir.mkdir()
    script = tmp_path / "bin" / "claude-jarvis"
    script.parent.mkdir()
    script.write_text(WRAPPER)
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("LAUNCH_LOG_DIR", str(logdir))

    class Handle:
        path = script

        @property
        def calls(self) -> list[dict]:
            return [json.loads(p.read_text()) for p in sorted(logdir.iterdir())]

    return Handle()


def test_an_open_template_is_parsed():
    got = session_dispatch.parse_launcher({"open": OPEN}, "where")
    assert got.open == tuple(OPEN)
    assert got.command == ()


def test_an_open_template_with_an_unknown_placeholder_is_refused():
    with pytest.raises(ValueError, match="where.*{bogus}"):
        session_dispatch.parse_launcher({"open": ["x", "{bogus}"]}, "where")


def test_a_launcher_needs_a_command_or_an_open_template():
    with pytest.raises(ValueError, match="where"):
        session_dispatch.parse_launcher({"env": {"A": "b"}}, "where")


def test_the_template_is_filled_from_the_order_and_its_briefing(tmp_path):
    lch = session_dispatch.parse_launcher({"open": OPEN}, "where")
    launch = session_dispatch.Launch(launcher=lch, project="p", project_path=tmp_path,
                                     wo_id="wo-1", wo_kind="worker", phase="open",
                                     transport="background")
    argv = launch.open_argv(prompt="do it", briefing={
        "model": "claude-opus-5", "autocompact_window": 400_000})
    assert argv == ["claude-jarvis", "start", "@jarvis", "--model", "claude-opus-5",
                    "--compact-pct", "40", "--prompt", "do it", "wo1"]


def test_wo_name_is_letters_and_digits_only():
    # claude-shining refuses any other name: `name must match ^[a-zA-Z0-9]+$`
    assert session_dispatch.wo_name("wo-23b7848c") == "wo23b7848c"


@pytest.mark.parametrize("window,pct", [(400_000, "40"), (None, "100"), (1_000, "1"),
                                        (2_000_000, "100")])
def test_compact_pct_is_the_window_as_a_share_of_the_model_context(window, pct):
    assert session_dispatch.compact_pct(window) == pct


def test_an_open_template_is_only_for_background_sessions(tmp_path):
    path = tmp_path / "catalog.json"
    path.write_text(json.dumps({"projects": [
        {"name": "p", "path": str(tmp_path),
         "worker": {"transport": "headless", "launcher": {"open": OPEN}}}]}))
    with pytest.raises(CatalogError, match="background"):
        load_catalog(path)


def test_an_open_template_opens_the_session_and_learns_its_worktree(
        fake_claude, wrapper, tmp_path):
    lch = session_dispatch.parse_launcher(
        {"open": [str(wrapper.path), *OPEN[1:]]}, "where")
    launch = session_dispatch.Launch(launcher=lch, project="p", project_path=tmp_path,
                                     wo_id="wo-tpl1", wo_kind="worker", phase="open",
                                     transport="background")
    entry = claude_cli.spawn_bg_open(launch.open_argv(prompt="go", briefing={
        "model": "claude-fake-1", "autocompact_window": 400_000}), tmp_path,
        env=launch.env())
    _wait_idle(entry.session_id)
    assert entry.cwd == str(Path(f"{tmp_path}.worktrees") / "wotpl1")
    assert fake_claude.turns[entry.session_id] == ["go"]
    [call] = wrapper.calls
    assert call["argv"][:3] == ["start", "@jarvis", "--model"]
    assert call["cwd"] == str(tmp_path)


@pytest.fixture()
def template_fleet(jarvis_home, fake_claude, catalog_file, project, wrapper, monkeypatch):
    data = json.loads(catalog_file.read_text())
    data["os"]["defaults"]["transport"] = "background"
    data["projects"][0].setdefault("worker", {})["launcher"] = {
        "open": [str(wrapper.path), *OPEN[1:]]}
    catalog_file.write_text(json.dumps(data))
    monkeypatch.setattr(worker_session, "BG_DEAD_GRACE", 0.0)
    ops.start_os(str(catalog_file), foreground=True)
    return load_catalog(catalog_file).projects[0], ProjectStore(project)


def test_a_templated_order_opens_through_the_wrapper_and_resumes_bare(
        template_fleet, wrapper, fake_claude, settle_turns):
    project, store = template_fleet
    wo = store.get_work_order(ops.create_work_order("proj_a", "task")["id"])

    worker_session.start(store, project, wo, "go")
    assert settle_turns(store)
    fresh = store.get_work_order(wo["id"])
    tree = Path(f"{project.path}.worktrees") / wo["id"].replace("-", "")
    assert Path(fresh["worktree"]) == tree
    assert worker_session.worktree_path(project, fresh) == tree

    worker_session.send(store, project, fresh, "again")
    assert settle_turns(store)

    assert len(wrapper.calls) == 1
    sid = fresh["session_id"]
    assert fake_claude.turns[sid] == ["go", "again"]
    [resume] = [c for c in fake_claude.calls if "--resume" in c["argv"]]
    assert resume["argv"] == ["--bg", "--resume", sid, "--", "again"]
    assert Path(resume["cwd"]).resolve() == tree.resolve()
    assert store.latest_turn(wo["id"])["result"] == "final: again"


def test_a_stale_banner_in_the_managers_output_never_binds_the_order(
        fake_claude, wrapper, tmp_path):
    old = claude_cli.spawn_bg_turn("old", tmp_path, "someone-else")
    _wait_idle(old.session_id)
    noisy = tmp_path / "bin" / "noisy"
    noisy.write_text(f"#!/usr/bin/env bash\necho 'backgrounded · {old.id} · stale'\n"
                     f"exec {wrapper.path} \"$@\"\n")
    noisy.chmod(noisy.stat().st_mode | stat.S_IEXEC)
    lch = session_dispatch.parse_launcher({"open": [str(noisy), *OPEN[1:]]}, "where")
    launch = session_dispatch.Launch(launcher=lch, project="p", project_path=tmp_path,
                                     wo_id="wo-new1", wo_kind="worker", phase="open",
                                     transport="background")

    entry = claude_cli.spawn_bg_open(launch.open_argv(prompt="go", briefing={}),
                                     tmp_path, env=launch.env())

    assert entry.session_id != old.session_id
    assert entry.cwd.endswith("wonew1")
