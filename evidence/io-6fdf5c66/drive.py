"""Run repro.py N times per interpreter and journal mode; print one line per run.

usage: /usr/bin/python3 -I drive.py <scratch-dir> <runs>
"""
import glob, os, subprocess, sys

HERE = os.path.dirname(os.path.abspath(__file__))
D, RUNS = sys.argv[1], int(sys.argv[2])
PYS = ["/home/gonzalo/miniconda3/bin/python3",
       "/home/gonzalo/.local/share/uv/python/cpython-3.13-linux-x86_64-gnu/bin/python3.13",
       "/home/gonzalo/.local/share/uv/python/cpython-3.11-linux-x86_64-gnu/bin/python3.11",
       "/usr/bin/python3.12"]
for py in PYS:
    ver = subprocess.run([py, "-I", "-c", "import sqlite3,sys;print(sys.version.split()[0],sqlite3.sqlite_version)"],
                         capture_output=True, text=True).stdout.strip()
    for mode in ("wal", "delete"):
        for run in range(1, RUNS + 1):
            for f in glob.glob(D + "/r.db*"):
                os.remove(f)
            try:
                r = subprocess.run([py, "-I", HERE + "/repro.py", D + "/r.db", mode],
                                   capture_output=True, text=True, timeout=40)
                out = (r.stdout + r.stderr).splitlines()
                ok = [l for l in out if l.startswith("ok")]
                stuck = [l.strip() for l in out if "repro.py\", line 2" in l]
                res = ok[0] if ok else "HUNG (faulthandler after 20s) at " + "; ".join(stuck)
            except subprocess.TimeoutExpired:
                res = "TIMEOUT"
            print(f"{py} [{ver}] {mode} run {run}: {res}", flush=True)
