"""A `deployed:` precondition clears itself (docs/superpowers/specs/2026-10-22-a-deployed-precondition-clears-itself.md)"""

from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from jarvis import cli, ops, preconditions
from jarvis.catalog import load_catalog
from jarvis.daemon import Daemon
from jarvis.project_store import ProjectStore

PR = "https://github.com/acme/proj/pull/7"
TAG = "jarvis-0.11.15"
OLD_TAG = "jarvis-0.11.14"

#: This repository, as `github.origin_repo` returns it for every checkout of it
OS_REPO = ("gonandrap", "agentic_os")


# -- fixtures


@pytest.fixture()
def started(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))


@pytest.fixture()
def store(project):
    s = ProjectStore(project)
    yield s
    s.close()


@pytest.fixture(autouse=True)
def os_identity(project, monkeypatch, origins):
    """`proj_a` IS the OS — `clear_deployed_precondition` runs on no other project"""
    from jarvis import schedule

    install = str(Path(schedule.__file__).resolve().parent)
    monkeypatch.setitem(schedule._ORIGIN_CACHE, install, OS_REPO)
    monkeypatch.setitem(schedule._ORIGIN_CACHE, str(project), OS_REPO)


@pytest.fixture()
def deploy(tmp_path, monkeypatch):
    """Put the production checkout at a tag, with a pyproject.toml version to match"""
    root = tmp_path / "production"
    monkeypatch.setenv("PRODUCTION_CODE", str(root))

    def put(source: Path, ref: str) -> None:
        dest = root / "jarvis_os"
        shutil.rmtree(dest, ignore_errors=True)
        root.mkdir(exist_ok=True)
        subprocess.run(["git", "clone", "-q", str(source), str(dest)], check=True)
        subprocess.run(["git", "-C", str(dest), "checkout", "-q", ref], check=True)
        (dest / "pyproject.toml").write_text(
            f'[project]\nname = "jarvis-os"\nversion = "{ref.removeprefix("jarvis-")}"\n')

    return put


def land(repo: Path, name: str) -> str:
    """One commit on `main`, as a squash merge leaves it"""
    from jarvis.testing import commit

    (repo / name).write_text(name)
    commit(repo, f"squash: {name}", name)
    out = subprocess.run(["git", "-C", str(repo), "rev-parse", "HEAD"],
                         capture_output=True, text=True, check=True)
    return out.stdout.strip()


def tag(repo: Path, name: str, rev: str = "HEAD") -> None:
    """An ANNOTATED tag, which is what the release path cuts"""
    subprocess.run(["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
                    "tag", "-a", name, "-m", name, rev], check=True)


def merged_dependency(store: ProjectStore, sha: str) -> str:
    """A completed work order whose PR put `sha` on `main`"""
    dep = store.create_work_order("ship the cache-TTL arm", status="completed")
    store.update_work_order(dep["id"], pr_url=PR)
    if sha:
        store.add_event(dep["id"], "pr_merged", {"pr_url": PR, "merge_commit": sha})
    return str(dep["id"])


# -- §2.1: the ref form


def test_the_target_is_canonicalised_and_an_empty_one_is_refused():
    assert preconditions.checked_ref("deployed: wo-19e58248 ") == "deployed:wo-19e58248"
    assert preconditions.deployed_target("deployed:wo-19e58248") == "wo-19e58248"
    assert preconditions.deployed_target("bl-d5baa28b") == ""

    with pytest.raises(preconditions.PreconditionError, match="deployed:"):
        preconditions.checked_ref("deployed:")


def test_the_flag_writes_the_row_and_the_order_is_not_claimed(started, store):
    dep = merged_dependency(store, "")
    wo = ops.create_work_order("proj_a", "read the A/B off the live fleet",
                               blocked_by=[f"deployed:{dep}"])

    assert [r["ref"] for r in store.open_preconditions(wo["id"])] == [f"deployed:{dep}"]
    assert store.claim_next_pending() is None


def test_the_cli_reaches_the_row_and_the_refusal_names_the_form(started, store, capsys):
    assert cli.main(["wo", "create", "proj_a", "read the A/B",
                     "--blocked-by", "deployed:wo-19e58248", "--json"]) == 0
    out = json.loads(capsys.readouterr().out)

    assert out["blocked_by"] == ["deployed:wo-19e58248"]
    assert [r["ref"] for r in store.open_preconditions(out["created"])] == [
        "deployed:wo-19e58248"]

    with pytest.raises(ops.OpsError) as e:
        ops.create_work_order("proj_a", "read it",
                              description="DO NOT START until the arm is deployed.")
    assert "deployed:<wo-id|sha>" in str(e.value)


# -- §2.2: the OS clears it


def test_the_hold_does_not_clear_while_the_dependency_has_not_merged(started, store,
                                                                    project, deploy):
    """THE REPRO's first half: no merge commit yet, so there is nothing to test"""
    sha = land(project, "arm.txt")
    tag(project, TAG, sha)
    deploy(project, TAG)
    dep = merged_dependency(store, "")          # completed, but no merge commit recorded
    wo = ops.create_work_order("proj_a", "read the A/B", blocked_by=[f"deployed:{dep}"])

    started.clear_settled_preconditions(started.catalog.projects[0], store)

    assert [r["ref"] for r in store.open_preconditions(wo["id"])] == [f"deployed:{dep}"]
    assert store.claim_next_pending() is None


def test_the_hold_does_not_clear_while_production_runs_an_older_tag(started, store,
                                                                    project, deploy):
    """ISSUE 1104's REPRO: merged to `main`, tagged, but not what the fleet runs"""
    tag(project, OLD_TAG)
    sha = land(project, "arm.txt")
    tag(project, TAG, sha)
    deploy(project, OLD_TAG)
    dep = merged_dependency(store, sha)
    wo = ops.create_work_order("proj_a", "read the A/B", blocked_by=[f"deployed:{dep}"])

    started.clear_settled_preconditions(started.catalog.projects[0], store)

    assert [r["ref"] for r in store.open_preconditions(wo["id"])] == [f"deployed:{dep}"]
    assert store.claim_next_pending() is None


def test_the_hold_clears_once_production_runs_a_tag_carrying_the_commit(
        started, store, project, deploy):
    sha = land(project, "arm.txt")
    tag(project, TAG, sha)
    deploy(project, TAG)
    dep = merged_dependency(store, sha)
    wo = ops.create_work_order("proj_a", "read the A/B", blocked_by=[f"deployed:{dep}"])

    started.clear_settled_preconditions(started.catalog.projects[0], store)

    assert store.open_preconditions(wo["id"]) == []
    assert store.conn.execute(
        "SELECT cleared_by FROM preconditions WHERE wo_id=?",
        (wo["id"],)).fetchone()["cleared_by"] == "os"
    assert store.claim_next_pending()["id"] == wo["id"]


def test_a_literal_sha_target_clears_on_the_same_condition(started, store, project,
                                                           deploy):
    sha = land(project, "arm.txt")
    tag(project, TAG, sha)
    deploy(project, TAG)
    wo = ops.create_work_order("proj_a", "read the A/B", blocked_by=[f"deployed:{sha}"])

    started.clear_settled_preconditions(started.catalog.projects[0], store)

    assert store.open_preconditions(wo["id"]) == []


def test_a_target_naming_no_work_order_stays_open(started, store, project, deploy):
    """§2.2: clearing on absence would release an order whose hold was never satisfied"""
    tag(project, TAG)
    deploy(project, TAG)
    wo = ops.create_work_order("proj_a", "read the A/B",
                               blocked_by=["deployed:wo-nosuchthing"])

    started.clear_settled_preconditions(started.catalog.projects[0], store)

    assert [r["ref"] for r in store.open_preconditions(wo["id"])] == [
        "deployed:wo-nosuchthing"]


# -- §2.3: `jarvis wo unblock`


def test_an_unresolvable_target_is_stranding_and_the_default_unblock_cuts_it(started,
                                                                            store):
    wo = ops.create_work_order("proj_a", "read the A/B",
                               blocked_by=["deployed:wo-nosuchthing"])

    out = ops.unblock_work_order(wo["id"])

    assert out["preconditions_cleared"] == ["deployed:wo-nosuchthing"]
    assert store.claim_next_pending()["id"] == wo["id"]


def test_a_resolvable_target_is_live_and_needs_all(started, store):
    dep = merged_dependency(store, "")
    ref = f"deployed:{dep}"
    wo = ops.create_work_order("proj_a", "read the A/B", blocked_by=[ref])

    rows = store.open_preconditions(wo["id"])
    assert ops._split_preconditions(rows, store) == ([ref], [])

    with pytest.raises(ops.OpsError) as e:
        ops.unblock_work_order(wo["id"])
    assert ref in str(e.value) and "--all" in str(e.value)

    out = ops.unblock_work_order(wo["id"], drop_all=True)
    assert out["preconditions_cleared"] == [ref]
    assert store.open_preconditions(wo["id"]) == []
