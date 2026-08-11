"""install_prod_service.sh — the systemd units, and the PATH the whole fleet inherits.

`Environment=PATH=` in the rendered unit is not a detail: it is the only PATH the
production daemon has, and the only one every worker it spawns inherits. A binary
missing from it is a feature that silently never happens — issue #90, where `gh` was
installed as a snap, `/snap/bin` was absent from the rendered PATH, and PR-merge
auto-completion was off for every project for a whole release without an error anywhere.

These drive the real script with `--dry-run`, pointed entirely at tmp_path, so nothing
is enabled, restarted or written into the developer's own ~/.config/systemd.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "install_prod_service.sh"

#: Enough of a PATH for the script's own tools (git, sed, python3). Deliberately does
#: NOT include /snap/bin: the point of most of these tests is what the script adds when
#: the PATH that invoked it is the stripped one a Jarvis worker actually has.
BASE_PATH = "/usr/local/bin:/usr/bin:/bin"


@pytest.fixture()
def prod(tmp_path: Path) -> Path:
    """A production root that looks deployed enough for the script to proceed."""
    venv_bin = tmp_path / "jarvis_os" / ".venv" / "bin"
    venv_bin.mkdir(parents=True)
    jarvis = venv_bin / "jarvis"
    jarvis.write_text("#!/bin/sh\nexit 0\n")
    jarvis.chmod(jarvis.stat().st_mode | stat.S_IEXEC)
    return tmp_path


def _run(prod_root: Path, tmp_path: Path, *,
         path: str = BASE_PATH) -> subprocess.CompletedProcess[str]:
    unit_dir = tmp_path / "units"
    env = {"PATH": path, "HOME": str(tmp_path / "home"),
           "PRODUCTION_CODE": str(prod_root), "USER": os.environ.get("USER", "t")}
    return subprocess.run(
        ["bash", str(SCRIPT), "--dry-run", "--unit-dir", str(unit_dir)],
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


# -- the PATH the daemon gets ---------------------------------------------------


def test_dry_run_renders_the_units_and_touches_no_systemd_state(prod, tmp_path):
    result = _run(prod, tmp_path)

    assert result.returncode == 0, result.stderr
    assert (tmp_path / "units" / "jarvis.service").is_file()
    assert (tmp_path / "units" / "jarvis-ui.service").is_file()
    for step in ("daemon-reload", "enable", "restart jarvis.service"):
        assert f"[dry-run] systemctl --user {step}" in result.stdout
    assert not (tmp_path / "home" / ".config").exists(), "wrote outside --unit-dir"


def test_snap_bin_is_on_the_service_path(prod, tmp_path):
    """Issue #90. `gh` is a snap on Ubuntu, and the probe below cannot find it when the
    PATH invoking this script is itself the stripped one — which it is whenever a Jarvis
    worker runs the installer. The fixed fallback list is what saves that case."""
    result = _run(prod, tmp_path)

    assert result.returncode == 0, result.stderr
    assert "/snap/bin" in _rendered_path(tmp_path).split(":")


def test_both_units_get_the_same_path(prod, tmp_path):
    """The dashboard shells out too, and a UI that can see `gh` while the daemon cannot
    is the confusing half-broken state."""
    _run(prod, tmp_path)

    assert _rendered_path(tmp_path) == _rendered_path(tmp_path, "jarvis-ui.service")


def test_gh_is_probed_wherever_it_actually_lives(prod, tmp_path):
    """The fallback list cannot enumerate every install location, so `gh` is probed the
    same way uv/claude/node are: find it on the invoking PATH, take its directory."""
    odd = tmp_path / "opt" / "weird" / "bin"
    _fake_gh(odd)

    result = _run(prod, tmp_path, path=f"{odd}:{BASE_PATH}")

    assert result.returncode == 0, result.stderr
    assert str(odd) in _rendered_path(tmp_path).split(":")


def test_the_prod_venv_still_comes_first(prod, tmp_path):
    """Regression guard on the ordering the PATH work must not disturb: `jarvis` inside
    a worker has to resolve to the production venv, not to a dev install in ~/.local."""
    _run(prod, tmp_path)

    entries = _rendered_path(tmp_path).split(":")
    assert entries[0] == str(prod / "jarvis_os" / ".venv" / "bin")


def test_the_fallback_list_mirrors_the_python_search_list(prod, tmp_path):
    """`bugreport.GH_SEARCH_DIRS` and this script's fallback list are two halves of one
    decision (Neo, question 86 on wo-238b9372); a comment in each says so. Drifting
    apart is how the self-healing scan starts looking in the wrong places."""
    from jarvis import bugreport
    _run(prod, tmp_path)

    entries = _rendered_path(tmp_path).split(":")
    # `~/.local/bin` is excluded: the script expands it against the caller's $HOME,
    # which this test has deliberately pointed at tmp_path.
    absolute = [d for d in bugreport.GH_SEARCH_DIRS if not d.startswith("~")]
    missing = [d for d in absolute if d not in entries]
    assert not missing, f"service PATH is missing {missing}"


# -- saying so when it is still broken ------------------------------------------


def test_the_script_reports_truthfully_whether_gh_is_reachable(prod, tmp_path):
    """Silence is what let #90 run for a release: the install succeeds either way, so
    the script has to state the outcome. Asserted against what the rendered PATH really
    resolves, since whether this host has a snap `gh` is not ours to decide."""
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
    """Rendering units that point at a venv with no `jarvis` in it installs a service
    that crash-loops."""
    result = _run(tmp_path / "empty", tmp_path)

    assert result.returncode != 0
    assert "not deployed yet" in result.stderr
