"""repro.py's WAL loop with a watchdog that SIGTRAPs itself after 12s, so gdb (which must
be the parent: kernel.yama.ptrace_scope=1 forbids attaching) stops and dumps native stacks.

usage: gdb -batch -ex run -ex "thread apply all bt 14" --args <python> -I gdb_repro.py <db-path>
"""
import os, signal, sqlite3, sys, threading, time

p = sys.argv[1]
c = sqlite3.connect(p, isolation_level=None)
c.execute("PRAGMA journal_mode=WAL")
c.execute("create table if not exists t(a)")
c.close()
stop = time.time() + 8


def loop():
    while time.time() < stop:
        x = sqlite3.connect(p)
        x.execute("select * from t").fetchall()
        x.close()


def watchdog():
    time.sleep(12)
    os.kill(os.getpid(), signal.SIGTRAP)


threading.Thread(target=watchdog, daemon=True).start()
ts = [threading.Thread(target=loop) for _ in (0, 1)]
[t.start() for t in ts]
[t.join() for t in ts]
print("ok: no hang")
