"""What the prefix fingerprint costs per session — the evidence behind finding 4's
second con, which is that the hook runs on every session for a condition that changes
rarely.

Two halves, because they are paid differently:

  BASELINE   one `jarvis _hook` process. Already spent on every SessionStart before any
             of this existed (`assets/settings.base.json`), so it is the denominator and
             not a cost this work added.
  ADDED      what `hooks.prefix_fingerprint` does inside that process: four small reads
             and a hash. The numerator.

    uv run python scripts/bench_hook_cost.py [repeats]
"""

from __future__ import annotations

import json
import statistics
import subprocess
import sys
import time
from pathlib import Path


def baseline(repeats: int) -> list[float]:
    """One `jarvis _hook` SessionStart, against a cwd that is not a managed project."""
    body = json.dumps({"hook_event_name": "SessionStart", "session_id": "bench",
                       "cwd": "/tmp", "source": "resume"})
    out = []
    for _ in range(repeats):
        start = time.perf_counter()
        subprocess.run(["jarvis", "_hook"], input=body, text=True,
                       capture_output=True, cwd="/tmp")
        out.append((time.perf_counter() - start) * 1000)
    return out


def added(repeats: int) -> list[float]:
    from jarvis.hooks import prefix_fingerprint

    root = Path.cwd()
    wo = {"id": "bench", "model": "claude-opus-5"}
    prefix_fingerprint(root, root, wo, {})  # warm the import, not the measurement
    out = []
    for _ in range(repeats):
        start = time.perf_counter()
        prefix_fingerprint(root, root, wo, {})
        out.append((time.perf_counter() - start) * 1000)
    return out


def main() -> None:
    repeats = int(sys.argv[1]) if len(sys.argv) > 1 else 21
    base = statistics.median(baseline(repeats))
    extra = statistics.median(added(repeats))
    print(f"baseline  `jarvis _hook` SessionStart   {base:8.2f} ms   (already spent)")
    print(f"added     prefix_fingerprint            {extra:8.2f} ms   "
          f"({extra / base:.2%} of it)")


if __name__ == "__main__":
    main()
