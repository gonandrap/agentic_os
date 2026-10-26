"""Which init system owns production's two services, and how launchd is asked (docs/superpowers/specs/2026-10-12-service-queries-on-a-mac.md)"""

from __future__ import annotations

import os
import subprocess
import time

import pytest

from jarvis import release, service_manager, systemd_units

LAUNCHCTL_PRINT = (
    "com.jarvis.ui = {\n"
    "\tstate = running\n"
    "\tpid = 73299\n"
    "\tlast exit code = 0\n"
    "}\n"
)


# -- which manager


@pytest.mark.parametrize("value,system,expected", [
    ("launchd", "Linux", service_manager.LAUNCHD),
    ("systemd", "Darwin", service_manager.SYSTEMD),
    ("", "Darwin", service_manager.LAUNCHD),
    ("", "Linux", service_manager.SYSTEMD),
])
def test_manager_resolves_the_env_first_then_uname(monkeypatch, value, system,
                                                   expected):
    """Same precedence and spelling as `install_prod_service.sh`, so installer and daemon never disagree"""
    monkeypatch.setenv(service_manager.MANAGER_ENV, value)
    monkeypatch.setattr(service_manager.platform, "system", lambda: system)

    assert service_manager.manager() == expected


@pytest.mark.parametrize("value", ["nonsense", "   ", "systemd launchd"])
def test_an_unrecognised_manager_reads_as_uname_and_never_raises(monkeypatch, value):
    """The shell scripts exit 2 on junk and that is right for an install a human is watching"""
    monkeypatch.setenv(service_manager.MANAGER_ENV, value)
    monkeypatch.setattr(service_manager.platform, "system", lambda: "Linux")

    assert service_manager.manager() == service_manager.SYSTEMD

    monkeypatch.setattr(service_manager.platform, "system", lambda: "Darwin")
    assert service_manager.manager() == service_manager.LAUNCHD


@pytest.mark.parametrize("value", ["LaunchD ", "\tLAUNCHD\n"])
def test_case_and_surrounding_space_are_normalised(monkeypatch, value):
    """Spec §1: `.strip().lower()` before the match"""
    monkeypatch.setenv(service_manager.MANAGER_ENV, value)
    monkeypatch.setattr(service_manager.platform, "system", lambda: "Linux")

    assert service_manager.manager() == service_manager.LAUNCHD


def test_the_unit_to_label_map_cannot_drift():
    """`service_manager` cannot import `release`, so its literal unit names are pinned here"""
    assert set(service_manager.LABELS) == {release.DAEMON_UNIT, release.UI_UNIT}


def test_a_unit_with_no_label_raises_naming_it():
    with pytest.raises(ValueError, match="jarvis-turn-wo-1-0.service"):
        service_manager.label("jarvis-turn-wo-1-0.service")


def test_target_is_the_domain_shell_builds():
    """`DOMAIN="gui/$UID"` — `install_prod_service.sh:126`, `shipit.sh:107`"""
    assert (service_manager.target(release.UI_UNIT)
            == f"gui/{os.getuid()}/com.jarvis.ui")


# -- the pure parsers


def test_parse_print_reads_the_captured_block():
    parsed = service_manager._parse_print(LAUNCHCTL_PRINT)

    assert parsed["state"] == "running"
    assert parsed["pid"] == "73299"
    assert parsed["last exit code"] == "0"


@pytest.mark.parametrize("text,expected", [
    ("05:12", 312.0),
    ("01:02:03", 3723.0),
    ("2-03:04:05", 2 * 86400 + 3 * 3600 + 4 * 60 + 5.0),
    ("  05:12  ", 312.0),
    ("", None),
    ("garbage", None),
])
def test_parse_etime(text, expected):
    assert service_manager._parse_etime(text) == expected


# -- unit_active


@pytest.mark.parametrize("state,expected", [
    ("running", True),
    ("waiting", True),      # counted the way systemd's branch counts `activating`
    ("exited", False),
    ("not running", False),
])
def test_unit_active_on_launchd(fake_launchd, state, expected):
    fake_launchd.load("com.jarvis.ui", state=state)

    assert service_manager.unit_active(release.UI_UNIT) is expected
    assert fake_launchd.calls[-1] == ["print", f"gui/{os.getuid()}/com.jarvis.ui"]


def test_an_absent_label_is_not_active_and_is_not_a_warning(fake_launchd, caplog):
    """A non-zero `launchctl print` is the NORMAL answer for a booted-out service"""
    caplog.set_level("DEBUG", logger="jarvisd.service")

    assert service_manager.unit_active(release.UI_UNIT) is False
    assert not [r for r in caplog.records if r.levelname == "WARNING"]


def test_unit_active_on_systemd_delegates_byte_identically(monkeypatch):
    """Delegates to `systemd_units.unit_active`, the Linux host's answer, rather than redefining it"""
    monkeypatch.setenv(service_manager.MANAGER_ENV, service_manager.SYSTEMD)
    seen: list[str] = []
    monkeypatch.setattr(systemd_units, "unit_active",
                        lambda unit: seen.append(unit) or True)

    assert service_manager.unit_active(release.UI_UNIT) is True
    assert seen == [release.UI_UNIT]


# -- start_time


def test_start_time_reads_a_pid_the_test_owns(fake_launchd):
    """`ps` is deliberately NOT stubbed: it is read-only"""
    # clock-exempt: class 4 — the instant compared against is the start time the kernel reports via `ps -o etime=`
    proc = subprocess.Popen(["sleep", "30"])
    try:
        fake_launchd.load("com.jarvis.ui", pid=proc.pid)
        before = time.time()

        started = service_manager.start_time(release.UI_UNIT)

        assert started is not None
        assert before - 10 <= started <= time.time() + 1
    finally:
        proc.kill()
        proc.wait()


def test_start_time_is_none_without_a_pid(fake_launchd):
    fake_launchd.load("com.jarvis.ui", state="waiting", pid=None)

    assert service_manager.start_time(release.UI_UNIT) is None


def test_start_time_is_none_when_the_job_is_not_loaded(fake_launchd):
    assert service_manager.start_time(release.UI_UNIT) is None


# -- restarts


def test_restart_unit_reloads_the_plist_so_a_re_render_takes_effect(fake_launchd):
    """`kickstart -k` keeps the loaded plist; only bootout + bootstrap reads the new one"""
    fake_launchd.load("com.jarvis.ui", pid=100)

    release.LaunchdRunner().restart_unit(release.UI_UNIT)

    assert fake_launchd.calls[-2:] == [
        ["bootout", f"gui/{os.getuid()}/com.jarvis.ui"],
        ["bootstrap", f"gui/{os.getuid()}", service_manager.plist_path(release.UI_UNIT)]]
    assert fake_launchd.jobs["com.jarvis.ui"]["pid"] == 101


def test_restart_unit_retries_a_bootstrap_launchd_refuses_while_the_old_job_exits(
        fake_launchd):
    """`Bootstrap failed: 5: Input/output error` is transient; the reload waits it out"""
    fake_launchd.load("com.jarvis.ui", pid=100)
    (fake_launchd.dir / "bootstrap-failures").write_text("2")

    release.LaunchdRunner().restart_unit(release.UI_UNIT)

    assert [c[0] for c in fake_launchd.calls].count("bootstrap") == 3
    assert fake_launchd.jobs["com.jarvis.ui"]["state"] == "running"


def test_restart_unit_detached_returns_at_once_and_outlives_our_session(fake_launchd):
    """`start_new_session=True` is load-bearing: launchd takes the job's process group down with it"""
    # clock-exempt: class 3 — spin-waits on a real detached subprocess that sleeps before it reloads
    fake_launchd.load("com.jarvis.daemon", pid=200)
    sessions: list[int] = []
    real_popen = subprocess.Popen

    def spy(argv, **kw):
        proc = real_popen(argv, **kw)
        sessions.append(os.getsid(proc.pid))
        return proc

    with pytest.MonkeyPatch.context() as mp:
        mp.setattr(service_manager.subprocess, "Popen", spy)
        started = time.time()
        release.LaunchdRunner().restart_unit_detached(release.DAEMON_UNIT, "jarvis-1.2.3")
        elapsed = time.time() - started

    assert elapsed < 1.0            # well under the 3-second sleep it queued
    assert sessions == [sessions[0]] and sessions[0] != os.getsid(os.getpid())

    deadline = time.time() + 20
    while time.time() < deadline and not any(
            c[:1] == ["bootstrap"] for c in fake_launchd.calls):
        time.sleep(0.2)
    assert fake_launchd.calls[-2:] == [
        ["bootout", f"gui/{os.getuid()}/com.jarvis.daemon"],
        ["bootstrap", f"gui/{os.getuid()}", service_manager.plist_path(release.DAEMON_UNIT)]]


def test_restart_hint_names_the_command_this_host_has(fake_launchd, monkeypatch):
    assert (service_manager.restart_hint(release.UI_UNIT)
            == f"launchctl kickstart -k gui/{os.getuid()}/com.jarvis.ui")

    monkeypatch.setenv(service_manager.MANAGER_ENV, service_manager.SYSTEMD)
    assert (service_manager.restart_hint(release.UI_UNIT)
            == "systemctl --user restart jarvis-ui.service")


# -- the factory, and the systemd path it must leave untouched


def test_service_runner_picks_the_manager(monkeypatch, fake_launchd):
    assert isinstance(release.service_runner(), release.LaunchdRunner)

    monkeypatch.setenv(service_manager.MANAGER_ENV, service_manager.SYSTEMD)
    assert isinstance(release.service_runner(), release.SystemdRunner)


def test_service_runner_resolves_the_class_as_a_module_global(monkeypatch):
    """Other suites rebind `release.SystemdRunner`, which works only if the lookup is at call time"""
    monkeypatch.setenv(service_manager.MANAGER_ENV, service_manager.SYSTEMD)

    class Fake:
        pass

    monkeypatch.setattr(release, "SystemdRunner", Fake)
    assert isinstance(release.service_runner(), Fake)


def test_the_systemd_argv_is_unchanged(monkeypatch):
    """Every systemd command the release path issues, byte for byte"""
    monkeypatch.setenv(service_manager.MANAGER_ENV, service_manager.SYSTEMD)
    runner = release.SystemdRunner()
    argvs: list[list[str]] = []
    monkeypatch.setattr(release.SystemdRunner, "_run",
                        lambda self, argv: argvs.append(argv) or "")

    runner.restart_unit(release.UI_UNIT)
    runner.restart_unit_detached(release.DAEMON_UNIT, "jarvis-1.2.3")
    runner.unit_start_time(release.DAEMON_UNIT)

    assert argvs[0] == ["systemctl", "--user", "restart", "jarvis-ui.service"]
    assert argvs[1] == [
        "systemd-run", "--user", "--collect",
        "--unit=jarvis-release-restart-jarvis-1-2-3",
        "--description=release jarvis-1.2.3: restart jarvis.service",
        "/bin/sh", "-c", "sleep 3; systemctl --user restart jarvis.service",
    ]
    assert argvs[2] == ["systemctl", "--user", "show", "jarvis.service",
                        "--property=ExecMainStartTimestampMonotonic", "--value"]


# -- INV-SERVICE-PATH reads the plists on a Mac


def _plist(path, env):
    import plistlib
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps({"Label": path.stem, "EnvironmentVariables": env}))


def test_unit_environment_reads_a_launchd_plist(fake_launchd):
    _plist(release.unit_dir() / "com.jarvis.daemon.plist", {"PATH": "/a:/b"})

    assert release.unit_environment(release.DAEMON_UNIT, "PATH") == "/a:/b"
    assert release.unit_environment(release.UI_UNIT, "PATH") is None


def test_service_path_drift_is_reported_from_the_plists(fake_launchd):
    from jarvis import bugreport, invariants

    gh_dir = os.path.dirname(bugreport.gh_bin())
    _plist(release.unit_dir() / "com.jarvis.daemon.plist", {"PATH": "/usr/bin:/bin"})
    _plist(release.unit_dir() / "com.jarvis.ui.plist", {"PATH": f"{gh_dir}:/usr/bin"})

    [violation] = list(invariants.check_service_path())
    assert violation.context["units"] == [release.DAEMON_UNIT]
