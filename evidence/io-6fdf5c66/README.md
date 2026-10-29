# io-6fdf5c66 evidence: libsqlite3 3.51.1 deadlocks WAL close vs open across threads

Host: Linux 7.0.0-30-generic, x86_64, 2026-10-29. No Jarvis code involved: plain `sqlite3`.

## Repro

`repro.py`: two threads, each looping `sqlite3.connect(p)`, `SELECT * FROM t`, `close()` on one file for 8s.
`drive.py` runs it 3 times per interpreter, in WAL and DELETE mode. A run that has not finished at 20s
is a hang (the loop stops at 8s) and faulthandler dumps where each thread sits.

    /usr/bin/python3 -I evidence/io-6fdf5c66/drive.py <scratch-dir> 3 > evidence/io-6fdf5c66/results.txt

Results, `results.txt` (iteration counts are per thread, in 8s):

| interpreter | SQLite | WAL | DELETE |
| --- | --- | --- | --- |
| miniconda CPython 3.13.12 (`/home/gonzalo/miniconda3/bin/python3`, the dev and prod venv interpreter) | 3.51.1 | HUNG 3/3, stuck at repro.py:21 (`connect`) and :23 (`close`) | ok 3/3, ~28,600-30,400 |
| uv-managed CPython 3.13.13 | 3.50.4 | ok 3/3, 8,214-11,642 | ok 3/3, ~28,200-29,900 |
| uv-managed CPython 3.11.15 | 3.50.4 | ok 3/3, 7,170-10,976 | ok 3/3, ~25,600-26,800 |
| /usr/bin/python3.12 (Ubuntu) | 3.45.1 | ok 3/3, 7,677-11,663 | ok 3/3, ~25,200-29,600 |

Without the SELECT (connect + close only), nothing hangs on any interpreter: the WAL index must be in use.

## Native stacks

`gdb_repro.py` is the WAL loop plus a watchdog that SIGTRAPs itself at 12s; gdb must be the parent
because `kernel.yama.ptrace_scope=1` forbids attaching.

    gdb -batch -ex run -ex "thread apply all bt 14" --args /home/gonzalo/miniconda3/bin/python3 -I evidence/io-6fdf5c66/gdb_repro.py <scratch>/g2.db

Excerpt of `gdb_backtrace.txt` (two of the frames 0-14 kept per thread; full file beside this one):

    Thread 4 (Thread 0x7ffff51a96c0 (LWP 2569912) "python3"):
    #3  ___pthread_mutex_lock (mutex=0x7fffe8001338) at ./nptl/pthread_mutex_lock.c:93
    #4  0x00007ffff71bfbce in findReusableFd () from /home/gonzalo/miniconda3/lib/python3.13/lib-dynload/../../libsqlite3.so.0
    #5  0x00007ffff723409b in unixOpen () from /home/gonzalo/miniconda3/lib/python3.13/lib-dynload/../../libsqlite3.so.0
    #8  ... pysqlite_connection_init_impl (...) at /usr/local/src/conda/python-3.13.12/Modules/_sqlite/connection.c:261
    Thread 3 (Thread 0x7ffff61aa6c0 (LWP 2569911) "python3"):
    #3  ___pthread_mutex_lock (mutex=0x7ffff72fefc8 <staticMutexes.103+360>) at ./nptl/pthread_mutex_lock.c:93
    #4  0x00007ffff71d9d20 in unixLock () from /home/gonzalo/miniconda3/lib/python3.13/lib-dynload/../../libsqlite3.so.0
    #5  0x00007ffff7235da0 in sqlite3WalClose () from /home/gonzalo/miniconda3/lib/python3.13/lib-dynload/../../libsqlite3.so.0
    #10 ... connection_close (self=0x7ffff74e98a0) at /usr/local/src/conda/python-3.13.12/Modules/_sqlite/connection.c:472

The close thread waits on a static SQLite mutex (`staticMutexes`); the open thread waits on a
heap-allocated per-inode mutex. Each holds the other's: a lock-order inversion. Both are
`pthread_mutex_lock`, which no busy handler governs, so `busy_timeout` (0 or 10s) and pragma
order cannot affect it.

## Upstream

https://www.sqlite.org/releaselog/3_51_2.html, 3.51.2 (2026-01-09):

> Fix an obscure deadlock in the new broken-posix-lock detection logic in item 17 above.

Item 17 is a 3.51.0 change: "Improved resistance to database corruption caused by an application
breaking Posix advisory locks using close()." So the affected range is 3.51.0-3.51.1.
I did not confirm in SQLite's source that this fix is exactly these two frames; the version range
and the repro results are what is established.
