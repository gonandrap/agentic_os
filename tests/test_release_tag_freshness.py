"""/release reads tags that exist only on origin (io-c1bbfff4, finding release-view-unfetched)"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

from jarvis import ops
from jarvis.central_store import CentralStore

pytestmark = pytest.mark.usefixtures("jarvis_home")


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True,
                          text=True).stdout.strip()


@pytest.fixture()
def clone(tmp_path, monkeypatch):
    """A checkout holding jarvis-0.1.0 while origin also has jarvis-0.2.0 on a release branch"""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", "-b", "main", str(origin))
    seed = tmp_path / "seed"
    seed.mkdir()
    _git(seed, "init", "-q", "-b", "main")
    _git(seed, "config", "user.email", "t@t")
    _git(seed, "config", "user.name", "t")
    _git(seed, "commit", "-q", "--allow-empty", "-m", "first (#1)")
    _git(seed, "tag", "jarvis-0.1.0")
    _git(seed, "remote", "add", "origin", str(origin))
    _git(seed, "push", "-q", "origin", "main", "jarvis-0.1.0")
    path = tmp_path / "clone"
    _git(tmp_path, "clone", "-q", str(origin), str(path))

    # shipit's shape: the bump commit and its tag live on release/*, never on main
    _git(seed, "checkout", "-q", "-b", "release/jarvis-0.2.0")
    _git(seed, "commit", "-q", "--allow-empty", "-m", "Release jarvis-0.2.0")
    _git(seed, "tag", "jarvis-0.2.0")
    _git(seed, "push", "-q", "origin", "release/jarvis-0.2.0", "jarvis-0.2.0")

    central = CentralStore()
    try:
        central.upsert_project("jarvis_os", str(path), "the OS")
        central.conn.commit()
    finally:
        central.close()
    real = ops._release_repo
    monkeypatch.setattr(ops, "_release_repo",
                        lambda project="": real(project) if project else ("jarvis_os", path))
    return path


def test_a_tag_only_on_origin_is_the_newest_release(clone):
    assert "jarvis-0.2.0" not in _git(clone, "tag")
    report = ops.release_overview()
    assert report["newest"] == "jarvis-0.2.0"
    assert [r["tag"] for r in report["releases"]] == ["jarvis-0.2.0", "jarvis-0.1.0"]
    assert report["refresh"]["ok"] is True
    assert ops.release_detail("jarvis-0.2.0")["tag"] == "jarvis-0.2.0"


def test_a_refresh_that_fails_still_renders_and_says_so(clone):
    _git(clone, "remote", "set-url", "origin", str(clone.parent / "gone.git"))
    report = ops.release_overview()
    assert report["newest"] == "jarvis-0.1.0"
    assert report["refresh"]["ok"] is False and report["refresh"]["error"]
    assert ops.release_detail("jarvis-0.1.0")["refresh"]["ok"] is False

    from fastapi.testclient import TestClient

    from jarvis.ui.app import create_app

    assert TestClient(create_app()).get("/release").status_code == 200
