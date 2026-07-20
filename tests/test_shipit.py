"""shipit release script — cuts a release from an ALREADY-merged main.

The correct process (mandated 2026-07-19): code lands on main via a reviewed PR;
shipit then cuts `release/jarvis-X.Y.Z` from main and does the version bump + tag
ON THE RELEASE BRANCH — main is never committed to. A Telegram notification fires
at the end. Version numbering derives from the latest `jarvis-*` tag (main's
pyproject is no longer bumped by shipit).

These drive the real scripts/shipit.sh in --dry-run against a throwaway repo, so
nothing is committed, deployed, or restarted.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

SHIPIT = Path(__file__).resolve().parents[1] / "scripts" / "shipit.sh"


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


def _make_repo(tmp_path: Path) -> Path:
    """A dev clone whose `origin` is a bare remote — git is the source of truth."""
    repo = tmp_path / "dev"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy(SHIPIT, repo / "scripts" / "shipit.sh")
    os.chmod(repo / "scripts" / "shipit.sh", 0o755)
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "jarvis-os"\nversion = "0.1.1"\n')
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Test")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")

    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", "-b", "main", str(origin)],
                   check=True, capture_output=True, text=True)
    _git(repo, "remote", "add", "origin", str(origin))
    _git(repo, "push", "-q", "-u", "origin", "main")
    return repo


def _dry_run(repo: Path, prod: Path, *args: str) -> subprocess.CompletedProcess[str]:
    env = {**os.environ, "PRODUCTION_CODE": str(prod)}
    return subprocess.run(
        ["bash", str(repo / "scripts" / "shipit.sh"), "--dry-run", *args],
        cwd=str(repo), env=env, capture_output=True, text=True)


def test_version_derives_from_latest_tag_not_pyproject(tmp_path):
    repo = _make_repo(tmp_path)
    # pyproject says 0.1.1, but the latest shipped tag is 0.1.2 → patch = 0.1.3
    _git(repo, "tag", "-a", "jarvis-0.1.0", "-m", "x")
    _git(repo, "tag", "-a", "jarvis-0.1.2", "-m", "x")
    r = _dry_run(repo, tmp_path / "prod", "patch")
    assert r.returncode == 0, r.stderr
    # base is the latest tag (0.1.2), NOT pyproject (0.1.1) → patch bump is 0.1.3
    assert "jarvis-0.1.3" in r.stdout
    assert "jarvis-0.1.2 " not in r.stdout  # not re-releasing the latest tag


def test_bump_and_tag_happen_on_release_branch_not_main(tmp_path):
    repo = _make_repo(tmp_path)
    _git(repo, "tag", "-a", "jarvis-0.1.2", "-m", "x")
    r = _dry_run(repo, tmp_path / "prod", "patch")
    assert r.returncode == 0, r.stderr
    out = r.stdout
    # release branch is cut from main first, and the bump commit targets it
    assert "git branch 'release/jarvis-0.1.3'" in out
    assert "release/jarvis-0.1.3" in out
    # main must never be committed to
    assert "committing on main" not in out
    assert "commit -m 'Release jarvis-0.1.3'" in out  # the commit is planned (on the branch)


def test_notifies_telegram(tmp_path):
    repo = _make_repo(tmp_path)
    r = _dry_run(repo, tmp_path / "prod", "0.2.0")
    assert r.returncode == 0, r.stderr
    assert "telegram" in r.stdout.lower()


def test_deploys_tag_and_restarts(tmp_path):
    repo = _make_repo(tmp_path)
    r = _dry_run(repo, tmp_path / "prod", "0.2.0")
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "checkout -f 'jarvis-0.2.0'" in out
    assert "jarvis-0.2.0" in out


def test_pushes_release_branch_and_tag_to_origin(tmp_path):
    """Git is the source of truth: nothing a release depends on stays local-only."""
    repo = _make_repo(tmp_path)
    r = _dry_run(repo, tmp_path / "prod", "0.2.0")
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert "push origin 'refs/heads/release/jarvis-0.2.0" in out
    assert "push origin 'refs/tags/jarvis-0.2.0'" in out


def test_deploys_from_the_git_remote_not_the_local_checkout(tmp_path):
    """Production tracks origin, so what runs in prod is what's on the remote."""
    repo = _make_repo(tmp_path)
    origin = str(tmp_path / "origin.git")
    r = _dry_run(repo, tmp_path / "prod", "0.2.0")
    assert r.returncode == 0, r.stderr
    out = r.stdout
    assert f"clone '{origin}'" in out
    assert f"remote set-url origin '{origin}'" in out
    assert f"clone '{repo}'" not in out


def test_refuses_when_main_is_ahead_of_origin(tmp_path):
    """An unpushed commit means the release wouldn't be reproducible from git."""
    repo = _make_repo(tmp_path)
    (repo / "local_only.txt").write_text("not pushed\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "local only")
    r = _dry_run(repo, tmp_path / "prod", "0.2.0")
    assert r.returncode != 0
    assert "origin/main" in (r.stdout + r.stderr)


def test_refuses_without_an_origin_remote(tmp_path):
    repo = _make_repo(tmp_path)
    _git(repo, "remote", "remove", "origin")
    r = _dry_run(repo, tmp_path / "prod", "0.2.0")
    assert r.returncode != 0
    assert "origin" in (r.stdout + r.stderr)


def test_explicit_version_is_respected(tmp_path):
    repo = _make_repo(tmp_path)
    _git(repo, "tag", "-a", "jarvis-0.1.2", "-m", "x")
    r = _dry_run(repo, tmp_path / "prod", "1.5.0")
    assert r.returncode == 0, r.stderr
    assert "jarvis-1.5.0" in r.stdout
