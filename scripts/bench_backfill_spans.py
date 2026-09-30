"""Measure `ProjectStore._backfill_wo_spans` on a SYNTHETIC project. Never a live one.

Builds a project of N work orders, each walked through a few statuses, then times the
gap-fill alone — the thing every CLI invocation and every reconcile tick pays for.

    uv run python scripts/bench_backfill_spans.py [orders] [repeats]
"""

from __future__ import annotations

import os
import statistics
import sys
import tempfile
import time
from pathlib import Path

WALK = ("dispatching", "running", "waiting_input", "running", "needs_review",
        "completed")


def build(root: Path, orders: int):
    from jarvis.project_store import ProjectStore

    store = ProjectStore(str(root))
    for i in range(orders):
        wo = store.create_work_order(f"order {i}")
        for status in WALK:
            store.set_status(wo["id"], status)
    store.conn.commit()
    return store


def main() -> None:
    orders = int(sys.argv[1]) if len(sys.argv) > 1 else 500
    repeats = int(sys.argv[2]) if len(sys.argv) > 2 else 20
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["JARVIS_HOME"] = str(Path(tmp) / "home")
        sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
        root = Path(tmp) / "proj"
        root.mkdir()
        store = build(root, orders)
        spans = store.conn.execute(
            "SELECT COUNT(*) c FROM wo_state_spans").fetchone()["c"]
        if "--gapped" in sys.argv:
            # Push every span behind its own status event, which is the state the guard
            # must NOT skip: the gap-fill runs in full.
            store.conn.execute("UPDATE wo_state_spans SET ts = ts - 1")
            store.conn.commit()
        samples = []
        for _ in range(repeats):
            start = time.perf_counter()
            store._backfill_wo_spans()
            samples.append((time.perf_counter() - start) * 1000)
        print(f"orders={orders} spans={spans} repeats={repeats}")
        print(f"median={statistics.median(samples):.3f}ms "
              f"mean={statistics.fmean(samples):.3f}ms "
              f"min={min(samples):.3f}ms max={max(samples):.3f}ms")
        store.close()


if __name__ == "__main__":
    main()
