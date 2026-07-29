"""install.sh — the curl-able installer new users land on.

The contract it has to keep:
  * installs a RELEASE TAG (jarvis-X.Y.Z), never `main`, even though the script itself
    is fetched from main;
  * picks the newest tag by NUMBER, so jarvis-0.1.10 wins over jarvis-0.1.9;
  * leaves an existing catalog alone (re-running it is the upgrade path);
  * tells the user how to onboard a project when it's done.

These drive the real install.sh in --dry-run against a throwaway local remote, so
nothing is downloaded, installed, or written outside tmp_path. The one test that
performs a REAL install is opt-in (JARVIS_TEST_INSTALL=1) because it needs uv/pipx
and the network.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
INSTALL_SH = REPO_ROOT / "install.sh"
README = REPO_ROOT / "README.md"

#: The URL the README (and the world) tells people to curl. Kept in one place so the
#: doc and the script can be checked against each other.
CURL_URL = "https://raw.githubusercontent.com/gonandrap/agentic_os/main/install.sh"


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout


def _remote_with_tags(tmp_path: Path, *tags: str) -> Path:
    """A bare repo standing in for GitHub, carrying the given tags."""
    work = tmp_path / "work"
    work.mkdir()
    (work / "pyproject.toml").write_text('[project]\nname = "jarvis-os"\nversion = "0.0.0"\n')
    _git(work, "init", "-q", "-b", "main")
    _git(work, "config", "user.email", "t@example.com")
    _git(work, "config", "user.name", "Test")
    _git(work, "add", "-A")
    _git(work, "commit", "-q", "-m", "init")
    for t in tags:
        _git(work, "tag", "-a", t, "-m", "x")
    bare = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(work), str(bare)],
                   check=True, capture_output=True, text=True)
    return bare


def _install(tmp_path: Path, *args: str, remote: Path | None = None,
             env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    """Run install.sh --dry-run, pointed entirely at tmp_path."""
    cmd = ["bash", str(INSTALL_SH), "--dry-run",
           "--bin-dir", str(tmp_path / "bin"),
           "--catalog", str(tmp_path / "home" / "catalog.json")]
    if remote is not None:
        cmd += ["--repo", str(remote)]
    cmd += list(args)
    return subprocess.run(cmd, capture_output=True, text=True,
                          env={**os.environ, "JARVIS_HOME": str(tmp_path / "home"),
                               **(env or {})})


def test_syntax_is_valid_bash():
    r = subprocess.run(["bash", "-n", str(INSTALL_SH)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


def test_is_executable():
    assert os.access(INSTALL_SH, os.X_OK), "install.sh must be chmod +x"


def test_help_works_without_a_script_file_on_disk():
    """`curl … | bash -s -- --help` has no $0 to read, so the help must be inline."""
    r = subprocess.run(["bash", "-s", "--", "--help"], stdin=INSTALL_SH.open(),
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    assert "--tag" in r.stdout and "--no-ui" in r.stdout


def test_installs_the_newest_release_tag_not_main(tmp_path):
    remote = _remote_with_tags(tmp_path, "jarvis-0.1.2", "jarvis-0.1.9")
    r = _install(tmp_path, remote=remote)
    assert r.returncode == 0, r.stderr
    assert "--branch 'jarvis-0.1.9'" in r.stdout
    assert "--branch 'main'" not in r.stdout


def test_newest_tag_is_compared_numerically(tmp_path):
    """Lexical sorting would pick 0.1.9 — the tenth patch release must win."""
    remote = _remote_with_tags(tmp_path, "jarvis-0.1.9", "jarvis-0.1.10")
    r = _install(tmp_path, remote=remote)
    assert r.returncode == 0, r.stderr
    assert "jarvis-0.1.10" in r.stdout
    assert "--branch 'jarvis-0.1.9'" not in r.stdout


def test_ignores_tags_that_are_not_releases(tmp_path):
    remote = _remote_with_tags(tmp_path, "jarvis-0.1.2", "jarvis-0.2.0-rc1", "v9", "nightly")
    r = _install(tmp_path, remote=remote)
    assert r.returncode == 0, r.stderr
    assert "--branch 'jarvis-0.1.2'" in r.stdout


def test_explicit_tag_is_respected_without_touching_the_remote(tmp_path):
    r = _install(tmp_path, "--tag", "jarvis-0.1.8", remote=tmp_path / "does-not-exist.git")
    assert r.returncode == 0, r.stderr
    assert "--branch 'jarvis-0.1.8'" in r.stdout


def test_fails_clearly_when_the_remote_has_no_releases(tmp_path):
    bare = tmp_path / "empty.git"
    subprocess.run(["git", "init", "-q", "--bare", str(bare)], check=True, capture_output=True)
    r = _install(tmp_path, remote=bare)
    assert r.returncode != 0
    assert "no jarvis-X.Y.Z release tags" in (r.stdout + r.stderr)


def test_dry_run_writes_nothing(tmp_path):
    remote = _remote_with_tags(tmp_path, "jarvis-1.0.0")
    r = _install(tmp_path, remote=remote)
    assert r.returncode == 0, r.stderr
    assert not (tmp_path / "bin").exists()
    assert not (tmp_path / "home").exists()


def test_installs_the_ui_extra_by_default_and_skips_it_on_demand(tmp_path):
    remote = _remote_with_tags(tmp_path, "jarvis-1.0.0")
    assert "jarvis_os[ui]" in _install(tmp_path, remote=remote).stdout
    assert "jarvis_os[ui]" not in _install(tmp_path, "--no-ui", remote=remote).stdout


def test_never_installs_over_an_existing_catalog(tmp_path):
    """Re-running the installer is the upgrade path; the fleet must survive it."""
    remote = _remote_with_tags(tmp_path, "jarvis-1.0.0")
    catalog = tmp_path / "home" / "catalog.json"
    catalog.parent.mkdir(parents=True)
    catalog.write_text('{"projects": [{"name": "mine", "path": "~/x"}]}')
    r = _install(tmp_path, remote=remote)
    assert r.returncode == 0, r.stderr
    assert "keeping your existing catalog" in r.stdout
    assert json.loads(catalog.read_text())["projects"][0]["name"] == "mine"


def test_warns_when_the_bin_dir_is_not_on_path(tmp_path):
    """Workers and hooks invoke `jarvis` by name — a silent off-PATH install is a trap."""
    remote = _remote_with_tags(tmp_path, "jarvis-1.0.0")
    r = _install(tmp_path, remote=remote, env={"PATH": "/usr/bin:/bin"})
    assert r.returncode == 0, r.stderr
    assert "not on your PATH" in (r.stdout + r.stderr)


def test_tells_the_user_how_to_onboard_a_project(tmp_path):
    remote = _remote_with_tags(tmp_path, "jarvis-1.0.0")
    out = _install(tmp_path, remote=remote).stdout
    assert "jarvis start --catalog" in out
    assert "jarvis wo create" in out
    assert "PROJECT_ONBOARDING.md" in out


def test_readme_leads_with_the_curl_one_liner():
    """A new user's first screen has to contain the install command."""
    first_screen = "\n".join(README.read_text().splitlines()[:30])
    assert CURL_URL in first_screen, "the curl one-liner must be in the README's first 30 lines"
    assert "curl -fsSL" in first_screen


def test_readme_does_not_point_at_the_pypi_name():
    """`jarvis-os` on PyPI is an unrelated project — those instructions installed the
    wrong software (see the work order's assumptions)."""
    text = README.read_text()
    assert "uv tool install jarvis-os" not in text
    assert "pipx install jarvis-os" not in text


@pytest.mark.skipif(not os.environ.get("JARVIS_TEST_INSTALL"),
                    reason="set JARVIS_TEST_INSTALL=1 to run a real install (needs uv/pipx)")
def test_real_install_from_a_tagged_remote(tmp_path):
    """End-to-end: tag this checkout as a release, install it, run the binary.

    Isolated by UV_TOOL_DIR/--bin-dir/JARVIS_HOME, so it cannot disturb the jarvis the
    developer actually uses.
    """
    if not shutil.which("uv") and not shutil.which("pipx"):
        pytest.skip("needs uv or pipx")
    src = tmp_path / "src"
    subprocess.run(["git", "clone", "-q", str(REPO_ROOT), str(src)],
                   check=True, capture_output=True)
    _git(src, "config", "user.email", "t@example.com")
    _git(src, "config", "user.name", "Test")
    pyproject = src / "pyproject.toml"
    pyproject.write_text("\n".join(
        'version = "9.9.9"' if line.startswith("version = ") else line
        for line in pyproject.read_text().splitlines()) + "\n")
    _git(src, "commit", "-qam", "release")
    _git(src, "tag", "-a", "jarvis-9.9.9", "-m", "x")
    remote = tmp_path / "origin.git"
    subprocess.run(["git", "clone", "-q", "--bare", str(src), str(remote)],
                   check=True, capture_output=True)

    bin_dir = tmp_path / "bin"
    r = subprocess.run(
        ["bash", str(INSTALL_SH), "--repo", str(remote), "--bin-dir", str(bin_dir),
         "--prefix", str(tmp_path / "prefix"), "--no-ui"],
        capture_output=True, text=True,
        env={**os.environ, "JARVIS_HOME": str(tmp_path / "home"),
             "UV_TOOL_DIR": str(tmp_path / "uvtools")})
    assert r.returncode == 0, r.stdout + r.stderr
    jarvis = bin_dir / "jarvis"
    assert jarvis.is_file()
    # the installed executable reports exactly the release that was installed
    ver = subprocess.run([str(jarvis), "--version"], capture_output=True, text=True)
    assert ver.returncode == 0, ver.stderr
    assert "9.9.9" in ver.stdout
    # ... and the starter catalog it wrote is one the OS accepts
    catalog = tmp_path / "home" / "catalog.json"
    doctor = subprocess.run([str(jarvis), "doctor", "--catalog", str(catalog)],
                            capture_output=True, text=True,
                            env={**os.environ, "JARVIS_HOME": str(tmp_path / "home")})
    assert doctor.returncode == 0, doctor.stdout + doctor.stderr
