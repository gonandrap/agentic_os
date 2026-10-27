"""install_prod_service.sh — the systemd units, and the PATH the whole fleet inherits (#90)"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "install_prod_service.sh"

#: Enough of a PATH for the script's own tools (git, sed, python3)
BASE_PATH = "/usr/local/bin:/usr/bin:/bin"


@pytest.fixture()
def prod(tmp_path: Path) -> Path:
    """A production root that looks deployed enough for the script to proceed"""
    venv_bin = tmp_path / "jarvis_os" / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    jarvis = venv_bin / "jarvis"
    jarvis.write_text("#!/bin/sh\nexit 0\n")
    jarvis.chmod(jarvis.stat().st_mode | stat.S_IEXEC)
    return tmp_path


def _run(prod_root: Path, tmp_path: Path, *, path: str = BASE_PATH,
         args: list[str] | None = None, manager: str | None = "systemd",
         ) -> subprocess.CompletedProcess[str]:
    unit_dir = tmp_path / "units"
    env = {"PATH": path, "HOME": str(tmp_path / "home"),
           "PRODUCTION_CODE": str(prod_root), "USER": os.environ.get("USER", "t")}
    # Pinned: the systemd tests below describe the Linux branch, on a Mac developer machine too
    if manager is not None:
        env["JARVIS_SERVICE_MANAGER"] = manager
    return subprocess.run(
        ["bash", str(SCRIPT), "--dry-run", "--unit-dir", str(unit_dir), *(args or [])],
        capture_output=True, text=True, env=env, check=False)


def _rendered_path(tmp_path: Path, unit: str = "jarvis.service") -> str:
    text = (tmp_path / "units" / unit).read_text()
    line = [ln for ln in text.splitlines() if ln.startswith("Environment=PATH=")]
    assert line, f"{unit} has no Environment=PATH= line at all"
    return line[0].split("=", 2)[2]


def _fake_gh(directory: Path) -> Path:
    directory.mkdir(parents=True, exist_ok=True)
    binpath = directory / "gh"
    binpath.write_text("#!/bin/sh\nexit 0\n")
    binpath.chmod(binpath.stat().st_mode | stat.S_IEXEC)
    return binpath


# -- the PATH the daemon gets


def test_dry_run_renders_the_units_and_touches_no_systemd_state(prod, tmp_path):
    result = _run(prod, tmp_path)

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "units" / "jarvis.service").is_file()
    assert (tmp_path / "units" / "jarvis-ui.service").is_file()
    for step in ("daemon-reload", "enable", "restart jarvis.service"):
        assert f"[dry-run] systemctl --user {step}" in result.stdout
    assert not (tmp_path / "home" / ".config").exists(), "wrote outside --unit-dir"


def test_snap_bin_is_on_the_service_path(prod, tmp_path):
    """Issue #90"""
    result = _run(prod, tmp_path)

    assert result.returncode == 0, result.stderr
    assert "/snap/bin" in _rendered_path(tmp_path).split(":")


def test_both_units_get_the_same_path(prod, tmp_path):
    """The dashboard shells out too, and a UI that can see `gh` while the daemon cannot is the confusing..."""
    _run(prod, tmp_path)

    assert _rendered_path(tmp_path) == _rendered_path(tmp_path, "jarvis-ui.service")


def test_gh_is_probed_wherever_it_actually_lives(prod, tmp_path):
    """The fallback list cannot enumerate every install location"""
    odd = tmp_path / "opt" / "weird" / "bin"
    _fake_gh(odd)

    result = _run(prod, tmp_path, path=f"{odd}:{BASE_PATH}")

    assert result.returncode == 0, result.stderr
    assert str(odd) in _rendered_path(tmp_path).split(":")


def test_the_prod_venv_still_comes_first(prod, tmp_path):
    """Regression guard on the ordering the PATH work must not disturb: `jarvis` inside a worker has to..."""
    _run(prod, tmp_path)

    entries = _rendered_path(tmp_path).split(":")
    assert entries[0] == str(prod / "jarvis_os" / ".venv" / "bin")


def test_the_fallback_list_mirrors_the_python_search_list(prod, tmp_path):
    """`bugreport.GH_SEARCH_DIRS` and this script's fallback list are two halves of one decision (Neo"""
    from jarvis import bugreport
    _run(prod, tmp_path)

    entries = _rendered_path(tmp_path).split(":")
    # `~/.local/bin` is excluded: the script expands it against the caller's $HOME
    absolute = [d for d in bugreport.GH_SEARCH_DIRS if not d.startswith("~")]
    missing = [d for d in absolute if d not in entries]
    assert not missing, f"service PATH is missing {missing}"


# -- saying so when it is still broken


def test_the_script_reports_truthfully_whether_gh_is_reachable(prod, tmp_path):
    """Silence is what let #90 run for a release: the install succeeds either way"""
    result = _run(prod, tmp_path)
    reachable = shutil.which("gh", path=_rendered_path(tmp_path))

    if reachable:
        assert "gh on the service PATH" in result.stdout
        assert "NOT on the service PATH" not in result.stderr
    else:
        assert "gh is NOT on the service PATH" in result.stderr


def test_a_probed_gh_makes_the_check_pass(prod, tmp_path):
    odd = tmp_path / "opt" / "weird" / "bin"
    _fake_gh(odd)

    result = _run(prod, tmp_path, path=f"{odd}:{BASE_PATH}")

    assert "gh on the service PATH" in result.stdout
    assert "NOT on the service PATH" not in result.stderr


def test_it_refuses_when_production_is_not_deployed(tmp_path):
    """Rendering units that point at a venv with no `jarvis` in it installs a service that crash-loops"""
    result = _run(tmp_path / "empty", tmp_path)

    assert result.returncode != 0
    assert "not deployed yet" in result.stderr


# -- --no-restart: the seam shipit re-renders through


def test_no_restart_reloads_but_leaves_the_restarts_to_the_caller(prod, tmp_path):
    """`shipit` re-renders on every release and already owns the restart ORDER"""
    result = _run(prod, tmp_path, args=["--no-restart"])

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "units" / "jarvis.service").is_file()
    assert "[dry-run] systemctl --user daemon-reload" in result.stdout
    assert "restart jarvis" not in result.stdout


# -- the doctor check that catches an install nobody re-ran


def _installed(unit_dir: Path, unit: str, path_value: str) -> None:
    unit_dir.mkdir(parents=True, exist_ok=True)
    (unit_dir / unit).write_text(
        f"[Service]\nEnvironment=JARVIS_HOME=/x\nEnvironment=PATH={path_value}\n")


@pytest.fixture()
def units(tmp_path, monkeypatch) -> Path:
    unit_dir = tmp_path / "installed-units"
    monkeypatch.setenv("JARVIS_SYSTEMD_UNIT_DIR", str(unit_dir))
    return unit_dir


def _violations(monkeypatch, gh: Path):
    from jarvis import bugreport, invariants
    monkeypatch.setenv(bugreport.GH_BIN_ENV, str(gh))
    return list(invariants.check_service_path())


def test_a_unit_whose_path_cannot_reach_gh_is_a_violation(units, tmp_path, monkeypatch):
    from jarvis import release
    gh = _fake_gh(tmp_path / "snap" / "bin")
    _installed(units, release.DAEMON_UNIT, BASE_PATH)

    found = _violations(monkeypatch, gh)

    assert [v.invariant for v in found] == ["INV-SERVICE-PATH"]
    assert found[0].context["units"] == [release.DAEMON_UNIT]
    assert str(gh.parent) in found[0].detail
    assert "install_prod_service.sh" in found[0].detail, "no way out is named"


def test_a_unit_that_reaches_gh_is_silent(units, tmp_path, monkeypatch):
    from jarvis import release
    gh = _fake_gh(tmp_path / "snap" / "bin")
    for unit in (release.DAEMON_UNIT, release.UI_UNIT):
        _installed(units, unit, f"{gh.parent}:{BASE_PATH}")

    assert _violations(monkeypatch, gh) == []


def test_it_reads_the_file_rather_than_the_running_process(units, tmp_path, monkeypatch):
    """`bugreport.heal_path` repairs the DAEMON's PATH at start-up"""
    from jarvis import release
    gh = _fake_gh(tmp_path / "snap" / "bin")
    _installed(units, release.DAEMON_UNIT, BASE_PATH)
    monkeypatch.setenv("PATH", f"{gh.parent}:{BASE_PATH}")

    assert len(_violations(monkeypatch, gh)) == 1


def test_no_units_installed_is_not_a_violation(units, tmp_path, monkeypatch):
    """Every dev checkout has no units at all; a doctor that shouts there is a doctor nobody runs"""
    gh = _fake_gh(tmp_path / "snap" / "bin")

    assert _violations(monkeypatch, gh) == []


def test_gh_missing_everywhere_is_someone_else_s_violation(units, monkeypatch):
    """A machine with no `gh` at all is a real problem, but not a STALE UNIT"""
    from jarvis import bugreport, invariants, release
    _installed(units, release.DAEMON_UNIT, BASE_PATH)
    monkeypatch.delenv(bugreport.GH_BIN_ENV, raising=False)
    monkeypatch.setattr(bugreport, "GH_SEARCH_DIRS", ())
    monkeypatch.setenv("PATH", "")

    assert list(invariants.check_service_path()) == []


def test_the_check_is_doctor_only(units):
    """`OS_INVARIANTS` runs on `jarvis doctor`"""
    from jarvis import invariants

    assert invariants.check_service_path in invariants.OS_INVARIANTS
    assert invariants.check_service_path not in invariants.INVARIANTS


# -- launchd: the same install on macOS


def _plist_string(tmp_path: Path, plist: str, key: str) -> str:
    text = (tmp_path / "units" / plist).read_text()
    marker = f"<key>{key}</key>"
    assert marker in text, f"{plist} has no {key} key"
    after = text.split(marker, 1)[1]
    return after.split("<string>", 1)[1].split("</string>", 1)[0]


def test_launchd_dry_run_renders_two_agents_and_never_calls_systemctl(prod, tmp_path):
    result = _run(prod, tmp_path, manager="launchd")

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "units" / "com.jarvis.daemon.plist").is_file()
    assert (tmp_path / "units" / "com.jarvis.ui.plist").is_file()
    assert "systemctl" not in result.stdout + result.stderr
    assert "[dry-run] launchctl bootstrap" in result.stdout
    assert not (tmp_path / "units" / "jarvis.service").exists()


def test_launchd_is_the_default_on_darwin(prod, tmp_path):
    """No knob needed on a Mac: `uname -s` decides, and the env var only overrides it"""
    result = _run(prod, tmp_path, manager=None)

    assert result.returncode == 0, result.stderr
    on_mac = os.uname().sysname == "Darwin"
    assert (tmp_path / "units" / "com.jarvis.daemon.plist").exists() == on_mac
    assert (tmp_path / "units" / "jarvis.service").exists() == (not on_mac)


def test_launchd_agents_carry_the_same_path_the_units_would(prod, tmp_path):
    odd = tmp_path / "opt" / "weird" / "bin"
    _fake_gh(odd)

    _run(prod, tmp_path, path=f"{odd}:{BASE_PATH}", manager="launchd")

    daemon_path = _plist_string(tmp_path, "com.jarvis.daemon.plist", "PATH")
    assert daemon_path == _plist_string(tmp_path, "com.jarvis.ui.plist", "PATH")
    entries = daemon_path.split(":")
    assert entries[0] == str(prod / "jarvis_os" / ".venv" / "bin")
    assert str(odd) in entries


def test_launchd_daemon_agent_runs_the_foreground_daemon_with_the_prod_catalog(prod, tmp_path):
    _run(prod, tmp_path, manager="launchd")

    text = (tmp_path / "units" / "com.jarvis.daemon.plist").read_text()
    assert "start --catalog" in text and "--foreground" in text
    assert str(prod / "config" / "catalog.json") in text
    assert f"<string>{prod / 'state'}</string>" in text  # JARVIS_HOME
    assert "<string>production</string>" in text         # JARVIS_ENV
    # launchd has no EnvironmentFile=; the secrets are sourced by the agent itself
    assert str(prod / "secrets" / "jarvis.env") in text


def test_launchd_ui_agent_serves_the_catalog_port(prod, tmp_path):
    (prod / "config").mkdir()
    (prod / "config" / "catalog.json").write_text('{"os": {"ui": {"port": 9797}}}')

    _run(prod, tmp_path, manager="launchd")

    text = (tmp_path / "units" / "com.jarvis.ui.plist").read_text()
    assert "<string>ui</string>" in text
    assert "<string>9797</string>" in text


def test_launchd_restarts_ui_inline_and_the_daemon_detached_and_last(prod, tmp_path):
    """Same ordering contract as the systemd branch: kick the UI here"""
    result = _run(prod, tmp_path, manager="launchd")

    out = result.stdout
    ui = out.index("launchctl kickstart -k gui/")
    assert "com.jarvis.ui" in out[ui:ui + 120]
    daemon = out.index("com.jarvis.daemon", ui)
    assert "detached" in out[daemon - 200:daemon + 200]
    assert ui < daemon


def test_launchd_no_restart_writes_the_plists_and_touches_no_agent(prod, tmp_path):
    """A bootout stops an agent and a bootstrap starts one, so either would restart production"""
    result = _run(prod, tmp_path, manager="launchd", args=["--no-restart"])

    assert result.returncode == 0, result.stderr
    assert "com.jarvis.daemon.plist" in result.stdout
    assert "bootout" not in result.stdout
    assert "bootstrap" not in result.stdout
    assert "kickstart" not in result.stdout
    assert "the caller reloads them" in result.stdout


def test_an_unknown_service_manager_is_refused(prod, tmp_path):
    result = _run(prod, tmp_path, manager="upstart")

    assert result.returncode != 0
    assert "JARVIS_SERVICE_MANAGER" in result.stderr
