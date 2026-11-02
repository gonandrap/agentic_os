"""libsqlite3 must not deadlock WAL close vs open across threads (kn-8e43df8e)"""
import subprocess
import sys

_LOOP = """
import sqlite3, sys, threading
path = sys.argv[1]
c = sqlite3.connect(path); c.execute("PRAGMA journal_mode=WAL"); c.execute("CREATE TABLE t(x)"); c.commit(); c.close()
def loop():
    for _ in range(3000):
        c = sqlite3.connect(path); c.execute("SELECT count(*) FROM t").fetchone(); c.close()
ts = [threading.Thread(target=loop) for _ in range(2)]
for t in ts: t.start()
for t in ts: t.join()
"""


def test_two_threads_connect_select_close_on_a_wal_file_do_not_deadlock(tmp_path):
    import sqlite3
    try:
        subprocess.run([sys.executable, "-c", _LOOP, str(tmp_path / "w.db")], timeout=20, check=True)
    except subprocess.TimeoutExpired:
        raise AssertionError(f"WAL connect/close deadlocked on SQLite {sqlite3.sqlite_version}") from None


def test_threads_racing_connect_on_a_fresh_file_all_get_wal(tmp_path):
    """Concurrent first WAL switch fails instantly without the retry (issue 805)"""
    import threading

    from jarvis import db

    errors = []

    def open_it(path):
        try:
            assert db.connect(path).execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        except Exception as e:  # noqa: BLE001
            errors.append(repr(e))

    for i in range(150):
        path = tmp_path / f"{i}.db"
        ts = [threading.Thread(target=open_it, args=(path,)) for _ in range(8)]
        for t in ts:
            t.start()
        for t in ts:
            t.join()
    assert errors == []


def test_jarvis_refuses_a_deadlocking_sqlite():
    import sqlite3

    import pytest

    from jarvis import db

    assert sqlite3.sqlite_version not in db.DEADLOCKING_SQLITE
    for bad in db.DEADLOCKING_SQLITE:
        with pytest.raises(RuntimeError, match="kn-8e43df8e"):
            db.refuse_deadlocking_sqlite(bad)
    db.refuse_deadlocking_sqlite("3.51.2")
