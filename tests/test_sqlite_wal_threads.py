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
