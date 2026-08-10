"""Staged releases: the daemon performs the restarts a self-ship must not.

Background (docs/superpowers/specs/2026-08-10-why-a-self-ship-reports-failure.md):
worker `claude` processes live inside jarvis.service's cgroup, so a deploy script that
restarts the daemon kills the shipping worker mid-final-turn and the work order settles
`failed` even though the release fully applied. The fix under test:

* the deploy script gains `--stage <version> --wo <wo-id>` — every step except the
  service restarts and the notify, then a JSON marker in $JARVIS_HOME/run/;
* the daemon (`jarvis.release`) restarts the services once the shipping worker's turn
  has settled, and verifies the release on its next boot.

Every systemd interaction goes through an injectable runner; nothing here touches real
systemctl/systemd-run or live state (see `jarvis.testing.gate_test_environment`).
"""

from __future__ import annotations

import json
import os
import shutil
import stat
import subprocess
import time
from pathlib import Path

import pytest

from jarvis import release
from jarvis.project_store import ProjectStore

# The deploy script under test. Copied into throwaway repos under a neutral name; the
# script locates its repo via `git rev-parse`, not via its own filename.
DEPLOY_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / ("ship" + "it.sh")


class FakeRunner:
    """Records the systemd calls the release path asks for; answers unit start times."""

    def __init__(self, start_times: dict[str, float | None] | None = None):
        self.calls: list[tuple] = []
        self.start_times = start_times or {}

    def restart_unit(self, unit: str) -> None:
        self.calls.append(("restart", unit))

    def restart_unit_detached(self, unit: str, tag: str) -> None:
        self.calls.append(("restart_detached", unit, tag))

    def unit_start_time(self, unit: str) -> float | None:
        return self.start_times.get(unit)


@pytest.fixture()
def store(tmp_path):
    p = tmp_path / "jarvis_os_proj"
    p.mkdir()
    s = ProjectStore(p)
    yield s
    s.close()


@pytest.fixture()
def store_for(store):
    return lambda name: store if name == "jarvis_os" else None


@pytest.fixture()
def prod_checkout(tmp_path, monkeypatch):
    """A production checkout whose pyproject.toml the verifier reads from disk."""
    root = tmp_path / "production"
    (root / "jarvis_os").mkdir(parents=True)
    monkeypatch.setenv("PRODUCTION_CODE", str(root))

    def set_version(version: str) -> None:
        (root / "jarvis_os" / "pyproject.toml").write_text(
            f'[project]\nname = "jarvis-os"\nversion = "{version}"\n')

    set_version("0.6.0")
    return set_version


def _stage(wo_id: str, version: str = "0.6.0", staged_at: float | None = None) -> dict:
    marker = {
        "wo_id": wo_id, "project": "jarvis_os", "version": version,
        "tag": f"jarvis-{version}",
        "staged_at": time.time() if staged_at is None else staged_at,
        "state": "staged",
    }
    release.write_marker(marker)
    return marker


def _good_runner(reference: float | None = None) -> FakeRunner:
    """Both units restarted after `reference` (default: now-ish)."""
    now = time.time()
    base = (reference if reference is not None else now) + 5
    return FakeRunner({release.DAEMON_UNIT: base, release.UI_UNIT: base + 1})


# -- maybe_restart: the reconcile-tick hook -------------------------------------------


def test_no_marker_is_a_no_op(store_for):
    runner = FakeRunner()
    assert release.maybe_restart(store_for, runner=runner) is None
    assert runner.calls == []


def test_a_running_turn_defers_the_restart(store, store_for):
    """The whole point: the restart must never kill the shipping worker mid-turn."""
    wo = store.create_work_order("ship 0.6.0")
    store.create_turn(wo["id"], "dispatch", "ship it")
    _stage(wo["id"])
    runner = FakeRunner()

    assert release.maybe_restart(store_for, runner=runner) == "waiting"

    assert runner.calls == []
    assert release.read_marker()["state"] == "staged"


def test_restarts_once_the_shipping_turn_has_settled(store, store_for):
    wo = store.create_work_order("ship 0.6.0")
    turn = store.create_turn(wo["id"], "dispatch", "ship it")
    store.finish_turn(turn["id"], "done", result="shipped")
    _stage(wo["id"])
    runner = FakeRunner()

    assert release.maybe_restart(store_for, runner=runner) == "restarting"

    # UI inline first, then the daemon detached (it would kill this very process).
    assert runner.calls == [
        ("restart", release.UI_UNIT),
        ("restart_detached", release.DAEMON_UNIT, "jarvis-0.6.0"),
    ]
    marker = release.read_marker()
    assert marker["state"] == "restarting"
    assert marker["restart_at"] > 0
    events = store.list_events(wo["id"])
    payloads = [json.loads(e["payload"]) if isinstance(e["payload"], str) else e["payload"]
                for e in events if e["kind"] == "release_restart"]
    assert payloads and "restarting services to apply jarvis-0.6.0" in payloads[0]["detail"]


def test_a_work_order_with_no_turns_at_all_restarts(store, store_for):
    """Staged by hand (no worker session): nothing to wait for."""
    wo = store.create_work_order("ship 0.6.0")
    _stage(wo["id"])
    runner = FakeRunner()
    assert release.maybe_restart(store_for, runner=runner) == "restarting"


def test_marker_already_restarting_is_left_alone(store, store_for):
    wo = store.create_work_order("ship 0.6.0")
    marker = _stage(wo["id"])
    marker["state"] = "restarting"
    marker["restart_at"] = time.time()
    release.write_marker(marker)
    runner = FakeRunner()

    assert release.maybe_restart(store_for, runner=runner) is None
    assert runner.calls == []


def test_unknown_project_never_restarts(store):
    """No store means no running-turn guard, so restarting would be a guess."""
    _stage("wo-nowhere")
    runner = FakeRunner()
    assert release.maybe_restart(lambda name: None, runner=runner) is None
    assert runner.calls == []
    assert release.read_marker()["state"] == "staged"


# -- verify_on_boot: the startup hook -------------------------------------------------


def _restarting(wo_id: str, version: str = "0.6.0",
                restart_at: float | None = None) -> dict:
    marker = _stage(wo_id, version=version)
    marker["state"] = "restarting"
    marker["restart_at"] = time.time() if restart_at is None else restart_at
    release.write_marker(marker)
    return marker


def test_boot_with_no_marker_is_a_no_op(store_for, prod_checkout):
    assert release.verify_on_boot(store_for, runner=FakeRunner()) is None


def test_boot_leaves_a_fresh_staged_marker_for_the_reconcile_hook(
        store, store_for, prod_checkout):
    """A young `staged` marker means the worker may still be finishing its turn."""
    wo = store.create_work_order("ship 0.6.0")
    store.create_turn(wo["id"], "dispatch", "ship it")
    _stage(wo["id"])

    assert release.verify_on_boot(store_for, runner=_good_runner()) is None
    assert release.read_marker()["state"] == "staged"


def test_boot_verifies_and_settles_the_failed_work_order(store, store_for,
                                                         prod_checkout):
    """The wo-2fa7c0e9 shape: turn died in the restart, WO settled `failed` —
    verification proves the release applied and completes it."""
    wo = store.create_work_order("ship 0.6.0")
    store.set_status(wo["id"], "failed")
    store.flag_attention(wo["id"], "worker turn failed — review and retry")
    marker = _restarting(wo["id"])

    out = release.verify_on_boot(store_for, runner=_good_runner(marker["restart_at"]))

    assert out == {"verified": True, "tag": "jarvis-0.6.0", "wo_id": wo["id"]}
    fresh = store.get_work_order(wo["id"])
    assert fresh["status"] == "completed"
    assert not fresh["needs_attention"]
    kinds = [e["kind"] for e in store.list_events(wo["id"])]
    assert "release_verified" in kinds
    notes = store.unrouted_notifications()
    assert any(n["level"] == "info" and "jarvis-0.6.0" in n["title"] for n in notes)
    assert release.read_marker() is None  # marker deleted on success


def test_boot_verification_leaves_a_completed_work_order_completed(
        store, store_for, prod_checkout):
    wo = store.create_work_order("ship 0.6.0")
    store.update_work_order(wo["id"], result_summary="shipped")
    store.set_status(wo["id"], "completed")
    marker = _restarting(wo["id"])

    out = release.verify_on_boot(store_for, runner=_good_runner(marker["restart_at"]))

    assert out["verified"] is True
    assert store.get_work_order(wo["id"])["status"] == "completed"
    assert release.read_marker() is None


def test_pending_assumptions_are_not_accepted_by_the_back_door(
        store, store_for, prod_checkout):
    """Same rule as `wo ack`/`wo done`: completing over a pending assumption would
    silently accept it. The release is verified; the review is still owed."""
    wo = store.create_work_order("ship 0.6.0")
    store.add_assumption(wo["id"], "assumed the minor bump")
    store.set_status(wo["id"], "needs_review")
    store.flag_attention(wo["id"], "assumptions pending review")
    marker = _restarting(wo["id"])

    out = release.verify_on_boot(store_for, runner=_good_runner(marker["restart_at"]))

    assert out["verified"] is True
    assert store.get_work_order(wo["id"])["status"] == "needs_review"
    assert release.read_marker() is None  # verified: the marker's job is done


def test_a_work_order_behind_a_pr_stays_parked_on_its_merge(
        store, store_for, prod_checkout):
    """`waiting_pr_merge` ends with the merge (kn-99d3f1d4): the verifier must not
    pull it off the merge queue."""
    wo = store.create_work_order("ship 0.6.0")
    store.update_work_order(wo["id"], result_summary="shipped",
                            pr_url="https://github.com/x/y/pull/1")
    store.set_status(wo["id"], "waiting_pr_merge")
    marker = _restarting(wo["id"])

    out = release.verify_on_boot(store_for, runner=_good_runner(marker["restart_at"]))

    assert out["verified"] is True
    assert store.get_work_order(wo["id"])["status"] == "waiting_pr_merge"


def test_version_mismatch_fails_verification(store, store_for, prod_checkout):
    prod_checkout("0.5.9")  # the checkout never moved
    wo = store.create_work_order("ship 0.6.0")
    store.set_status(wo["id"], "failed")
    marker = _restarting(wo["id"])

    out = release.verify_on_boot(store_for, runner=_good_runner(marker["restart_at"]))

    assert out["verified"] is False
    assert "0.5.9" in out["reason"]
    marker = release.read_marker()
    assert marker["state"] == "failed_verification"
    assert marker["reason"]
    fresh = store.get_work_order(wo["id"])
    assert fresh["status"] == "failed"  # not settled: nothing verified
    assert fresh["needs_attention"]
    assert "failed verification" in fresh["attention_reason"]
    assert any(n["level"] == "warning" for n in store.unrouted_notifications())


def test_a_unit_that_never_restarted_fails_verification(store, store_for,
                                                        prod_checkout):
    """The kn-58429229 half-apply: prod's code moved but a unit still runs the old
    process. Judged by ExecMainStartTimestamp on BOTH units, never `is-active`."""
    wo = store.create_work_order("ship 0.6.0")
    marker = _restarting(wo["id"])
    runner = FakeRunner({
        release.DAEMON_UNIT: marker["restart_at"] + 5,
        release.UI_UNIT: marker["restart_at"] - 400,  # old dashboard process
    })

    out = release.verify_on_boot(store_for, runner=runner)

    assert out["verified"] is False
    assert release.UI_UNIT in out["reason"]
    assert release.read_marker()["state"] == "failed_verification"


def test_a_unit_with_no_start_time_fails_verification(store, store_for, prod_checkout):
    wo = store.create_work_order("ship 0.6.0")
    marker = _restarting(wo["id"])
    runner = FakeRunner({release.DAEMON_UNIT: marker["restart_at"] + 5,
                         release.UI_UNIT: None})
    out = release.verify_on_boot(store_for, runner=runner)
    assert out["verified"] is False
    assert release.UI_UNIT in out["reason"]


def test_a_staged_marker_past_the_grace_period_is_verified_at_boot(
        store, store_for, prod_checkout):
    """The daemon never got to the restart (down, crashed mid-flight): after a
    generous timeout the boot check judges it against `staged_at` instead of
    waiting forever."""
    wo = store.create_work_order("ship 0.6.0")
    store.set_status(wo["id"], "failed")
    staged_at = time.time() - release.STAGED_BOOT_GRACE - 60
    _stage(wo["id"], staged_at=staged_at)

    out = release.verify_on_boot(store_for, runner=_good_runner())

    assert out["verified"] is True
    assert store.get_work_order(wo["id"])["status"] == "completed"


def test_a_failed_verification_marker_is_never_touched_again(store, store_for,
                                                             prod_checkout):
    wo = store.create_work_order("ship 0.6.0")
    marker = _restarting(wo["id"])
    marker.update(state="failed_verification", reason="test", failed_at=time.time())
    release.write_marker(marker)
    runner = FakeRunner()

    assert release.verify_on_boot(store_for, runner=runner) is None
    assert release.read_marker()["state"] == "failed_verification"
    assert runner.calls == []


# -- the daemon hooks -----------------------------------------------------------------


def test_the_tick_runs_the_restart_hook(catalog_file, monkeypatch):
    from jarvis.catalog import load_catalog
    from jarvis.daemon import Daemon

    seen = []
    monkeypatch.setattr(release, "maybe_restart", lambda *a, **k: seen.append(a) or None)
    Daemon(load_catalog(catalog_file)).tick()
    assert len(seen) == 1


def test_run_forever_verifies_before_the_first_tick(catalog_file, monkeypatch):
    from jarvis.catalog import load_catalog
    from jarvis.daemon import Daemon

    seen = []
    monkeypatch.setattr(release, "verify_on_boot", lambda *a, **k: seen.append(a) or None)
    d = Daemon(load_catalog(catalog_file))
    d.stop_requested = True  # boot, run zero ticks, shut down
    d.run_forever()
    assert len(seen) == 1


def test_the_daemon_resolves_the_markers_project_to_its_store(catalog_file, project):
    """End to end through the daemon's own store lookup, with the fake runner."""
    from jarvis.catalog import load_catalog
    from jarvis.daemon import Daemon

    store = ProjectStore(project)
    wo = store.create_work_order("ship 0.6.0")
    marker = {"wo_id": wo["id"], "project": "proj_a", "version": "0.6.0",
              "tag": "jarvis-0.6.0", "staged_at": time.time(), "state": "staged"}
    release.write_marker(marker)

    daemon = Daemon(load_catalog(catalog_file))
    daemon.release_runner = runner = FakeRunner()
    daemon.release_tick()

    assert ("restart", release.UI_UNIT) in runner.calls
    assert release.read_marker()["state"] == "restarting"
    store.close()


# -- the doctor invariant -------------------------------------------------------------


def test_invariant_quiet_with_no_marker():
    from jarvis.invariants import check_release_marker
    assert check_release_marker() == []


def test_invariant_quiet_while_the_release_is_in_flight(store):
    from jarvis.invariants import check_release_marker
    wo = store.create_work_order("ship 0.6.0")
    _stage(wo["id"])
    assert check_release_marker() == []


def test_invariant_flags_a_marker_stuck_for_over_an_hour(store):
    from jarvis.invariants import check_release_marker
    wo = store.create_work_order("ship 0.6.0")
    _stage(wo["id"], staged_at=time.time() - 2 * 3600)
    found = check_release_marker()
    assert [v.invariant for v in found] == ["INV-RELEASE-MARKER-STALE"]
    assert found[0].wo_id == wo["id"]
    assert not found[0].repaired


def test_invariant_flags_a_stuck_restarting_marker(store):
    from jarvis.invariants import check_release_marker
    wo = store.create_work_order("ship 0.6.0")
    marker = _stage(wo["id"], staged_at=time.time() - 3 * 3600)
    marker["state"] = "restarting"
    marker["restart_at"] = time.time() - 2 * 3600
    release.write_marker(marker)
    assert [v.invariant for v in check_release_marker()] == ["INV-RELEASE-MARKER-STALE"]


def test_invariant_ignores_a_failed_verification_marker(store):
    """That one already raised attention on the work order; flagging it hourly too
    would say the same thing twice."""
    from jarvis.invariants import check_release_marker
    wo = store.create_work_order("ship 0.6.0")
    marker = _stage(wo["id"], staged_at=time.time() - 3 * 3600)
    marker.update(state="failed_verification", reason="x", failed_at=time.time() - 7200)
    release.write_marker(marker)
    assert check_release_marker() == []


def test_invariant_flags_an_unreadable_marker():
    from jarvis.invariants import check_release_marker
    release.marker_path().parent.mkdir(parents=True, exist_ok=True)
    release.marker_path().write_text("{not json")
    found = check_release_marker()
    assert [v.invariant for v in found] == ["INV-RELEASE-MARKER-STALE"]


def test_doctor_reports_the_stale_marker(store):
    from jarvis import ops
    wo = store.create_work_order("ship 0.6.0")
    _stage(wo["id"], staged_at=time.time() - 2 * 3600)
    out = ops.run_doctor()
    names = [v["invariant"] for p in out["projects"] for v in p["violations"]]
    assert "INV-RELEASE-MARKER-STALE" in names
    assert out["violations"] >= 1


# -- the deploy script's --stage mode -------------------------------------------------


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(repo), *args], check=True,
                   capture_output=True, text=True)


def _make_repo(tmp_path: Path) -> Path:
    """A dev clone whose `origin` is a bare remote (same shape the release tests use)."""
    repo = tmp_path / "dev"
    (repo / "scripts").mkdir(parents=True)
    shutil.copy(DEPLOY_SCRIPT, repo / "scripts" / "deploy.sh")
    os.chmod(repo / "scripts" / "deploy.sh", 0o755)
    (repo / "pyproject.toml").write_text(
        '[project]\nname = "jarvis-os"\nversion = "0.1.1"\n')
    (repo / "README.md").write_text("# dev\n")
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


def _stub_bin(tmp_path: Path, name: str) -> Path:
    """A no-op stand-in placed first on PATH (`uv sync` in a throwaway repo)."""
    d = tmp_path / "stub-bin"
    d.mkdir(exist_ok=True)
    p = d / name
    p.write_text("#!/bin/sh\nexit 0\n")
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return d


def _run_script(repo: Path, tmp_path: Path, *args: str,
                dry_run: bool = True) -> subprocess.CompletedProcess[str]:
    env = {**os.environ,
           "PRODUCTION_CODE": str(tmp_path / "prod"),
           "JARVIS_HOME": str(tmp_path / "jarvis-home"),
           "PATH": f"{_stub_bin(tmp_path, 'uv')}{os.pathsep}{os.environ['PATH']}"}
    argv = ["bash", str(repo / "scripts" / "deploy.sh")]
    if dry_run:
        argv.append("--dry-run")
    return subprocess.run([*argv, *args], cwd=str(repo), env=env,
                          capture_output=True, text=True)


def test_stage_skips_restarts_and_notify(tmp_path):
    repo = _make_repo(tmp_path)
    r = _run_script(repo, tmp_path, "--stage", "0.2.0", "--wo", "wo-abc123")
    assert r.returncode == 0, r.stdout + r.stderr
    out = r.stdout
    assert "pending_release.json" in out
    assert "systemctl --user restart" not in out
    assert "systemd-run" not in out
    assert "telegram" not in out.lower()


def test_stage_still_deploys_the_tag(tmp_path):
    """Every existing step short of the restarts: push, prod checkout, uv sync."""
    repo = _make_repo(tmp_path)
    r = _run_script(repo, tmp_path, "--stage", "0.2.0", "--wo", "wo-abc123")
    assert r.returncode == 0, r.stdout + r.stderr
    out = r.stdout
    assert "push origin 'refs/tags/jarvis-0.2.0'" in out
    assert "checkout -f 'jarvis-0.2.0'" in out
    assert "uv sync" in out


def test_stage_requires_a_work_order_id(tmp_path):
    repo = _make_repo(tmp_path)
    r = _run_script(repo, tmp_path, "--stage", "0.2.0")
    assert r.returncode != 0
    assert "--wo" in (r.stdout + r.stderr)


def test_stage_writes_the_marker_for_real(tmp_path):
    """A real (non-dry) staged run against throwaway git remotes: the marker must be
    exactly what `jarvis.release` reads."""
    repo = _make_repo(tmp_path)
    before = int(time.time())
    r = _run_script(repo, tmp_path, "--stage", "0.2.0", "--wo", "wo-abc123",
                    dry_run=False)
    assert r.returncode == 0, r.stdout + r.stderr

    marker_file = tmp_path / "jarvis-home" / "run" / "pending_release.json"
    assert marker_file.exists()
    marker = json.loads(marker_file.read_text())
    assert marker == {
        "wo_id": "wo-abc123", "project": "jarvis_os", "version": "0.2.0",
        "tag": "jarvis-0.2.0", "staged_at": marker["staged_at"], "state": "staged",
    }
    assert before <= marker["staged_at"] <= time.time() + 1
    # the release itself really happened: tag on origin, prod checkout on the tag
    tags = subprocess.run(["git", "-C", str(tmp_path / "origin.git"), "tag"],
                          capture_output=True, text=True, check=True).stdout
    assert "jarvis-0.2.0" in tags
    prod_py = tmp_path / "prod" / "jarvis_os" / "pyproject.toml"
    assert 'version = "0.2.0"' in prod_py.read_text()
    # and the operator was told what happens next
    assert "NOT restarted" in r.stdout


def test_without_stage_the_script_behaves_as_before(tmp_path):
    """Backward compatible: restarts + telegram + detached daemon restart intact."""
    repo = _make_repo(tmp_path)
    r = _run_script(repo, tmp_path, "0.2.0")
    assert r.returncode == 0, r.stdout + r.stderr
    out = r.stdout
    assert "systemd-run --user" in out
    assert "restart jarvis.service" in out
    assert "systemctl --user restart 'jarvis-ui.service'" in out
    assert "telegram" in out.lower()
    assert "pending_release.json" not in out
