"""Two threads loop connect, SELECT, close on one WAL file. Hangs on SQLite 3.51.0-3.51.1.

usage: python -I repro.py <db-path> wal|delete
Prints 'ok <python> <sqlite> <mode> [iterations per thread] in 8s' or, if stuck, a
faulthandler dump after 20s (the loop itself stops after 8s, so a dump means a hang).
"""
import faulthandler, sqlite3, sys, threading, time

faulthandler.dump_traceback_later(20, exit=True)
p, mode = sys.argv[1], sys.argv[2]
c = sqlite3.connect(p, isolation_level=None)
c.execute("PRAGMA journal_mode=" + ("WAL" if mode == "wal" else "DELETE"))
c.execute("create table if not exists t(a)")
c.close()
stop = time.time() + 8
n = [0, 0]


def loop(i):
    while time.time() < stop:
        x = sqlite3.connect(p)
        x.execute("select * from t").fetchall()
        x.close()
        n[i] += 1


ts = [threading.Thread(target=loop, args=(i,)) for i in (0, 1)]
[t.start() for t in ts]
[t.join() for t in ts]
print("ok", sys.version.split()[0], sqlite3.sqlite_version, mode, n, "in 8s")
