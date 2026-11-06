"""`gitrefs`: the one layer that refreshes a clone before the OS reads refs from it (io-c1bbfff4)"""

import subprocess
from pathlib import Path

import pytest

from jarvis import gitrefs, morpheus, release, release_view


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True,
                          capture_output=True, text=True).stdout


@pytest.fixture(autouse=True)
def _forget_refreshes():
    gitrefs.forget()
    yield
    gitrefs.forget()


@pytest.fixture
def origin(tmp_path) -> Path:
    repo = tmp_path / "origin"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    _git(repo, "commit", "-q", "--allow-empty", "-m", "first")
    _git(repo, "tag", "jarvis-0.1.0")
    return repo


def _tag_on_a_release_branch(origin: Path, tag: str) -> None:
    """What `shipit` does: the bump commit and its tag live on `release/<tag>`, never on main"""
    _git(origin, "checkout", "-q", "-b", f"release/{tag}", "main")
    _git(origin, "commit", "-q", "--allow-empty", "-m", f"Release {tag}")
    _git(origin, "tag", tag)
    _git(origin, "checkout", "-q", "main")


@pytest.fixture
def clone(tmp_path, origin) -> Path:
    path = tmp_path / "clone"
    subprocess.run(["git", "clone", "-q", str(origin), str(path)], check=True,
                   capture_output=True, text=True)
    return path


def test_a_tag_cut_on_another_machine_is_visible_to_a_read(clone, origin):
    _tag_on_a_release_branch(origin, "jarvis-0.2.0")
    assert [t["tag"] for t in release_view.tags(clone)][0] == "jarvis-0.2.0"


def test_the_refresh_is_cached_per_repo_until_it_is_forced(clone, origin):
    release_view.tags(clone)
    _tag_on_a_release_branch(origin, "jarvis-0.2.0")
    assert [t["tag"] for t in release_view.tags(clone)][0] == "jarvis-0.1.0"
    assert gitrefs.refresh(clone, force=True).ok
    assert [t["tag"] for t in release_view.tags(clone)][0] == "jarvis-0.2.0"


def test_a_clone_that_cannot_reach_origin_still_answers_and_reports_the_failure(origin):
    assert [t["tag"] for t in release_view.tags(origin)] == ["jarvis-0.1.0"]
    failed = gitrefs.refresh(origin)
    assert not failed.ok and failed.error


def test_tags_containing_forces_a_fetch_past_a_cached_refresh(clone, origin):
    sha = _git(clone, "rev-parse", "HEAD").strip()
    release_view.tags(clone)
    _tag_on_a_release_branch(origin, "jarvis-0.2.0")
    found, detail = release.tags_containing(clone, [sha])
    assert "jarvis-0.2.0" in found, detail


def test_a_fetch_failure_names_no_token_the_remote_url_carried(tmp_path):
    repo = tmp_path / "tokened"
    repo.mkdir()
    _git(repo, "init", "-q", "-b", "main")
    _git(repo, "remote", "add", "origin",
         "https://x-access-token:SECRET@127.0.0.1:1/o/r.git")
    error = gitrefs.refresh(repo).error
    assert error and "SECRET" not in error


def test_scrub_redacts_the_userinfo_of_a_url_git_does_print_back():
    assert gitrefs.scrub("fatal: https://x-access-token:SECRET@github.com/o/r.git gone") == (
        "fatal: https://<redacted>@github.com/o/r.git gone")


def test_fetch_ref_refuses_a_ref_that_is_not_a_ref_name(clone):
    assert not gitrefs.fetch_ref(clone, "--upload-pack=false")
    assert not gitrefs.fetch_ref(clone)


def test_fetch_ref_brings_one_branch_down_and_counts_as_no_tag_refresh(clone, origin):
    _git(origin, "commit", "-q", "--allow-empty", "-m", "second")
    _tag_on_a_release_branch(origin, "jarvis-0.2.0")
    assert gitrefs.fetch_ref(clone, "origin/main")
    assert _git(clone, "log", "-1", "--format=%s", "origin/main").strip() == "second"
    assert [t["tag"] for t in release_view.tags(clone)][0] == "jarvis-0.2.0"


def test_previous_tag_refuses_to_guess_at_a_tag_the_clone_does_not_have(clone):
    assert morpheus.previous_tag(clone, "jarvis-0.1.0") == ""
    with pytest.raises(morpheus.UnknownTag):
        morpheus.previous_tag(clone, "jarvis-9.9.9")
