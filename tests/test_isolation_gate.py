"""The test-isolation gate: a test run must not be able to reach production.

Two escape routes existed, and both fired for real on 2026-07-27. A worker session
inherits `JARVIS_HOME=~/workspace/production/state`, so:

1. any test that touched central state wrote PRODUCTION `os.db` — including the central
   inbox, which the live daemon drains; and
2. the live daemon then routed those items to the real sinks, so two Telegram alerts for
   the fixture project `proj_a` landed on the user's phone, one of them a deep link that
   500'd because the work order did not exist outside the test's tmp dir.

The gate closes both: the repo-root conftest redirects `JARVIS_HOME` into a throwaway
sandbox before collection (and the autouse `jarvis_home` fixture gives every test its own
home on top of that), and the sinks with effects outside this process refuse to fire
while `JARVIS_DISABLE_EXTERNAL_SINKS` is set.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from jarvis import bugreport, claude_cli, notify, paths, testing
from jarvis.catalog import parse_catalog
from jarvis.central_store import CentralStore

REPO_ROOT = Path(__file__).resolve().parent.parent

#: A test that drives the real `Daemon` against central state. Before the gate it created
#: `os.db`, `logs/` and `run/` in whatever `JARVIS_HOME` the shell happened to carry.
LEAKY_TEST = "tests/test_invariants.py::test_the_daemon_repairs_and_records_on_its_tick"


# -- JARVIS_HOME can never be the ambient one ----------------------------------------


def test_the_process_floor_home_is_a_throwaway_sandbox():
    """What code running outside a test body sees: collection, module import, a
    subprocess spawned before any fixture ran."""
    assert testing.GATE_ROOT is not None and testing.GATE_HOME is not None
    assert testing.GATE_HOME.is_relative_to(testing.GATE_ROOT)
    assert testing.GATE_HOME != Path("~/.jarvis").expanduser()
    assert testing.GATE_ROOT.name.startswith("jarvis-test-gate-")


def test_jarvis_home_is_never_the_users_real_home():
    home = paths.jarvis_home()

    assert home != Path("~/.jarvis").expanduser()
    assert not home.is_relative_to(Path.home() / "workspace"), (
        "JARVIS_HOME still points inside a real checkout — production state is reachable")


def test_every_test_gets_its_own_home_without_asking_for_it(tmp_path):
    """The `jarvis_home` fixture is autouse, so a test that never names it is still
    isolated per-test — no shared sandbox for forgetful tests to collide in."""
    assert paths.jarvis_home() == tmp_path / "jarvis-home"


def test_naming_the_fixture_gets_the_same_home_the_gate_already_installed(jarvis_home):
    assert paths.jarvis_home() == jarvis_home


def test_a_test_that_forgets_the_fixture_cannot_write_the_ambient_home(tmp_path):
    """End-to-end: run the real leaky test in a hostile shell and prove nothing lands.

    The child env is stripped of the gate's own variables, so this proves the child run
    re-establishes isolation for itself rather than inheriting ours.
    """
    poisoned = tmp_path / "pretend-production"
    poisoned.mkdir()
    env = {k: v for k, v in os.environ.items()
           if k not in (notify.DISABLE_EXTERNAL_SINKS_ENV, bugreport.GH_BIN_ENV,
                        bugreport.BUG_REPO_ENV)}
    env["JARVIS_HOME"] = str(poisoned)

    proc = subprocess.run(
        [sys.executable, "-m", "pytest", LEAKY_TEST, "-q", "-p", "no:cacheprovider"],
        cwd=REPO_ROOT, env=env, capture_output=True, text=True, timeout=300,
    )

    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert list(poisoned.iterdir()) == [], (
        f"the test run wrote to the ambient JARVIS_HOME: "
        f"{[p.name for p in poisoned.iterdir()]}"
    )


# -- the sinks that reach outside this process refuse to fire -------------------------


def _inbox_item(**over) -> dict:
    item = {"id": 1, "ts": 0, "project": "proj_a", "level": "critical",
            "title": "test default: gate reviews escalate unless forced", "body": "",
            "wo_id": "wo-deadbeef"}
    item.update(over)
    return item


def _catalog(sinks: list[str]):
    return parse_catalog({"os": {"notifications": {"sinks": sinks}}, "projects": []})


def test_external_sinks_are_disabled_for_the_whole_suite():
    assert os.environ.get(notify.DISABLE_EXTERNAL_SINKS_ENV) == "1"


def test_telegram_refuses_to_send_even_with_credentials_in_the_environment(monkeypatch):
    monkeypatch.setenv("JARVIS_TELEGRAM_TOKEN", "123:real-looking-token")
    monkeypatch.setenv("JARVIS_TELEGRAM_CHAT_ID", "4242")

    result = notify.sink_telegram(_inbox_item(), _catalog(["telegram"]))

    assert result == f"skipped: {notify.DISABLE_EXTERNAL_SINKS_ENV} set"


def test_the_desktop_sink_is_disabled_too():
    assert notify.sink_desktop(_inbox_item(), _catalog(["desktop"])) == (
        f"skipped: {notify.DISABLE_EXTERNAL_SINKS_ENV} set")


def test_the_log_sink_still_works_because_it_stays_inside_the_sandbox(jarvis_home):
    assert notify.sink_log(_inbox_item(), _catalog(["log"])) == "ok"
    assert (jarvis_home / "logs" / "notifications.log").exists()


def test_routing_a_real_inbox_item_cannot_reach_telegram(monkeypatch, jarvis_home):
    """The exact incident shape: an inbox item for the fixture project, routed."""
    monkeypatch.setenv("JARVIS_TELEGRAM_TOKEN", "123:real-looking-token")
    monkeypatch.setenv("JARVIS_TELEGRAM_CHAT_ID", "4242")
    catalog = _catalog(["telegram", "desktop"])
    central = CentralStore()
    central.add_inbox(project="proj_a", level="critical", title="gate review",
                      body="", wo_id="wo-deadbeef")

    assert notify.route_new_inbox(central, catalog) == 1
    item = central.unacked_inbox()[0]
    results = json.loads(item["sink_results"])
    central.close()

    assert results["telegram"].startswith("skipped:")
    assert results["desktop"].startswith("skipped:")
    assert results["log"] == "ok"


def test_a_disabled_sink_still_marks_the_item_notified():
    """The router must not retry forever just because a sink was gated off."""
    central = CentralStore()
    central.add_inbox(project="proj_a", level="info", title="gated", body="")
    notify.route_new_inbox(central, _catalog(["telegram"]))
    remaining = central.new_inbox()
    central.close()

    assert remaining == []


# -- the OS's own bug tracker is a production surface too ----------------------------


def test_filing_a_bug_from_a_test_cannot_reach_the_real_tracker():
    """Without the `fake_gh` fixture, `gh` must be a blocked stub, not the real CLI —
    otherwise a stray test files a public issue on the OS's own tracker.

    The gate deliberately does NOT override `JARVIS_BUG_REPO`: blocking the binary is
    what makes the tracker unreachable, and shadowing the default repo name would
    silently change what tests/test_bugreport.py asserts about it.
    """
    assert bugreport.gh_bin() != "gh"
    assert Path(bugreport.gh_bin()).is_relative_to(testing.GATE_ROOT or Path("/nowhere"))

    with pytest.raises(bugreport.BugReportError) as e:
        bugreport.create_issue("gate probe", "body", bugreport.DEFAULT_BUG_REPO)

    assert "blocked by the Jarvis test-isolation gate" in str(e.value)


def test_the_fake_gh_fixture_still_wins_over_the_gate(fake_gh):
    url = bugreport.create_issue("title", "body", "example/repo")

    assert url == fake_gh.issue_url


# -- and the real model, which costs real money --------------------------------------


def test_a_test_that_forgets_fake_claude_cannot_spawn_a_real_agent():
    assert claude_cli.claude_bin() != "claude"

    with pytest.raises(claude_cli.ClaudeCliError) as e:
        claude_cli.version()

    assert "blocked by the Jarvis test-isolation gate" in str(e.value)


def test_the_fake_claude_fixture_still_wins_over_the_gate(fake_claude):
    assert claude_cli.version() == "9.9.9 (fake claude)"


def test_the_llm_evals_can_opt_back_into_the_real_model(tmp_path, monkeypatch):
    """`JARVIS_EVALS_LLM=1` means "this run asked for the real model" — the gate must
    not stand in the way of evals/llm, which exist to grade real model output.

    Asserted against `gate_environment`, which computes the gate's env without applying
    it, so probing the gate cannot disturb the gate this run is relying on.
    """
    monkeypatch.setenv(testing.LLM_EVALS_ENV, "1")

    env = testing.gate_environment(tmp_path / "probe-gate")

    assert claude_cli.CLAUDE_BIN_ENV not in env
    # every other route stays shut: opting into the model is not opting into the world
    assert env[notify.DISABLE_EXTERNAL_SINKS_ENV] == "1"
    assert env[bugreport.GH_BIN_ENV].endswith("/gh")
    assert env["JARVIS_HOME"].startswith(str(tmp_path))


def test_without_the_llm_flag_the_real_model_is_blocked(tmp_path, monkeypatch):
    monkeypatch.delenv(testing.LLM_EVALS_ENV, raising=False)

    env = testing.gate_environment(tmp_path / "probe-gate")

    assert env[claude_cli.CLAUDE_BIN_ENV].endswith("/claude")


# -- the gate is structural, not per-suite opt-in ------------------------------------


def test_the_gate_is_installed_from_the_repo_root_conftest():
    """Tripwire. The gate lives in the repo-root conftest so pytest applies it to every
    suite under the rootdir — tests/, evals/, tests_browser/ and anything added later.
    Moving it into a per-suite conftest would let the next suite opt out by omission."""
    root_conftest = REPO_ROOT / "conftest.py"

    assert root_conftest.exists(), "the repo-root conftest.py IS the gate"
    body = root_conftest.read_text()
    assert "gate_test_environment" in body
    assert "def pytest_configure" in body


@pytest.mark.parametrize("suite", ["tests", "evals", "tests_browser"])
def test_every_suite_sits_under_the_rootdir_the_gate_covers(suite):
    assert (REPO_ROOT / suite).is_dir()
    assert (REPO_ROOT / "pyproject.toml").exists(), "rootdir anchor for conftest lookup"
