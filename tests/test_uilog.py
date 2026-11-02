"""The dashboard's log — writing it, and (the point) reading it back"""

from __future__ import annotations

import time

from jarvis import db, uilog


def _boom(msg: str = "kaboom") -> Exception:
    try:
        raise RuntimeError(msg)
    except RuntimeError as e:
        return e


def test_sigusr1_dumps_every_thread_to_stderr():
    import subprocess
    import sys

    code = ("import os, signal; from jarvis import uilog; uilog.register_stack_signal(); "
            "os.kill(os.getpid(), signal.SIGUSR1); print('alive')")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=30)
    assert out.stdout.strip() == "alive"
    assert "most recent call first" in out.stderr


def test_error_round_trips_through_the_log(jarvis_home):
    uilog.record_error("GET", "/wo/proj_a/wo-1", _boom("no such thing"))

    errors, cursor = uilog.read_errors()
    assert len(errors) == 1
    e = errors[0]
    assert (e.method, e.path, e.exc_type) == ("GET", "/wo/proj_a/wo-1", "RuntimeError")
    assert e.message == "no such thing"
    assert "RuntimeError: no such thing" in e.traceback
    assert cursor.startswith(f"{uilog.ui_log_path().stat().st_size}:")


def test_traceback_lines_can_never_forge_an_entry(jarvis_home):
    """The parser keys on column-0 header lines, so every traceback line is indented"""
    stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime())
    uilog.record_error("GET", "/x", _boom(f"{stamp} [ERROR] GET /forged — Fake: hi"))

    errors, _ = uilog.read_errors()
    assert len(errors) == 1
    assert errors[0].path == "/x"
    # The newline-free flattening is what keeps the forged text on the header line
    assert "/forged" in errors[0].message


def test_read_errors_resumes_from_the_offset(jarvis_home):
    """Exactly-once alerting: the daemon stores the offset so a standing error is not re-announced on..."""
    uilog.record_error("GET", "/one", _boom("first"))
    first, cursor = uilog.read_errors()
    assert len(first) == 1

    assert uilog.read_errors(cursor) == ([], cursor)

    uilog.record_error("GET", "/two", _boom("second"))
    second, cursor2 = uilog.read_errors(cursor)
    assert [e.path for e in second] == ["/two"]
    assert cursor2 != cursor


def test_a_replaced_log_restarts_from_the_top_instead_of_going_silent(jarvis_home):
    """A bare byte offset is not enough: rotation can leave a *same-sized* new file"""
    uilog.record_error("GET", "/one", _boom("first"))
    _, cursor = uilog.read_errors()

    uilog.ui_log_path().write_text("")  # as if rotated out from under us
    uilog.record_error("GET", "/two", _boom("second"))

    errors, _ = uilog.read_errors(cursor)
    assert [e.path for e in errors] == ["/two"]


def _write_entry(when: str, path: str) -> None:
    """One well-formed entry at an arbitrary timestamp"""
    with uilog.ui_log_path().open("a") as f:
        f.write(f"{when} [ERROR] GET {path} — RuntimeError: stale\n"
                "    Traceback (most recent call last):\n")


def _stamp(seconds_ago: float) -> str:
    return time.strftime(uilog._STAMP, time.localtime(db.now() - seconds_ago))


def test_a_cold_start_does_not_announce_a_log_full_of_history(jarvis_home):
    """The 0.5.5 regression: `check_ui_log` shipped a release *after* the log writer"""
    uilog.ui_log_path().parent.mkdir(parents=True, exist_ok=True)
    for i in range(4):
        _write_entry(_stamp(7 * 24 * 3600), f"/neo{i}")

    errors, cursor = uilog.read_errors()

    assert errors == []
    # The cursor still advances past them: they are read and dismissed
    assert cursor.startswith(f"{uilog.ui_log_path().stat().st_size}:")


def test_a_cold_start_still_announces_a_recent_error(jarvis_home):
    """The bound is the entries' age, not the absence of a cursor"""
    uilog.ui_log_path().parent.mkdir(parents=True, exist_ok=True)
    _write_entry(_stamp(7 * 24 * 3600), "/ancient")
    _write_entry(_stamp(60), "/still-broken")

    errors, _ = uilog.read_errors()

    assert [e.path for e in errors] == ["/still-broken"]


def test_a_replaced_log_re_announces_only_what_is_still_recent(jarvis_home):
    """The age bound and the restart-from-the-top rule have to compose: rotation puts the reader back at..."""
    uilog.record_error("GET", "/one", _boom("first"))
    _, cursor = uilog.read_errors()

    uilog.ui_log_path().write_text("")  # rotated out from under us
    _write_entry(_stamp(7 * 24 * 3600), "/ancient")
    uilog.record_error("GET", "/two", _boom("second"))

    errors, _ = uilog.read_errors(cursor)
    assert [e.path for e in errors] == ["/two"]


def test_the_window_boundary_is_decided_on_one_clock(jarvis_home, clock):
    """Writer and readers decide the window on one clock, or it moves by a whole day (fo-2891ead5 §7.1)"""
    with clock.moved(-(uilog.ERROR_WINDOW_SECONDS + 1)):
        uilog.record_error("GET", "/past-the-cutoff", _boom("a second too old"))
    with clock.moved(-(uilog.ERROR_WINDOW_SECONDS - 1)):
        uilog.record_error("GET", "/inside-the-cutoff", _boom("a second too young"))

    errors, _ = uilog.read_errors()
    assert [e.path for e in errors] == ["/inside-the-cutoff"]

    recent, total = uilog.recent_errors()
    assert ([e.path for e in recent], total) == (["/inside-the-cutoff"], 1)


def test_the_log_rotates_instead_of_growing_without_bound(jarvis_home, monkeypatch):
    """A dashboard stuck in a crash loop must not fill the state directory"""
    monkeypatch.setattr(uilog, "MAX_BYTES", 400)
    for i in range(40):
        uilog.record_error("GET", f"/p{i}", _boom(f"boom {i}"))

    assert uilog.ui_log_path().stat().st_size < 4 * uilog.MAX_BYTES
    assert uilog.ui_log_path().with_name("ui.log.1").exists()


def test_recent_errors_reports_the_window_and_the_newest_first(jarvis_home):
    for i in range(3):
        uilog.record_error("GET", f"/p{i}", _boom(f"boom {i}"))

    recent, total = uilog.recent_errors(limit=2)
    assert total == 3
    assert [e.path for e in recent] == ["/p2", "/p1"]


def test_errors_age_out_of_the_reporting_window(jarvis_home):
    """The standing "the dashboard is broken" signal has to expire on its own"""
    uilog.record_error("GET", "/old", _boom("ancient"))
    assert uilog.recent_errors(within_seconds=-1) == ([], 0)


def test_an_error_clears_once_its_route_serves_a_success_again(jarvis_home):
    """wo-c93acc14: a fixed 'disk is full' kept NEEDS YOU lit for the whole 24h window"""
    uilog.ui_log_path().parent.mkdir(parents=True, exist_ok=True)
    _write_entry(_stamp(120), "/")
    _write_entry(_stamp(120), "/wo/p/wo-1")
    with uilog.access_log_path().open("a") as f:
        f.write(f"{_stamp(180)} [200] GET /wo/p/wo-1 5ms\n")  # before the error: not a recovery
        f.write(f"{_stamp(60)} [500] GET /wo/p/wo-1 5ms\n")
        f.write(f"{_stamp(60)} [200] GET /?tab=x 5ms\n")

    recent, total = uilog.recent_errors()
    assert total == 1
    assert [e.path for e in recent] == ["/wo/p/wo-1"]


def test_access_lines_record_path_query_and_status(jarvis_home):
    uilog.record_access("GET", "/wo/proj_a/wo-1?error=nope", 404, 12.4)
    line = uilog.access_log_path().read_text()
    assert "[404] GET /wo/proj_a/wo-1?error=nope 12ms" in line


def test_logging_never_raises_even_when_the_log_dir_is_unusable(jarvis_home,
                                                                monkeypatch):
    """A logger that can take the dashboard down is worse than no logger"""
    monkeypatch.setattr(uilog, "ui_log_path", lambda: jarvis_home / "logs")  # a dir
    uilog.record_error("GET", "/x", _boom())  # must not raise
