#!/usr/bin/env python3
"""Offline A/B: does graph expansion (arm B) find what workers went on to read?

    sqlite3 -readonly $PROD/state/os.db ".backup /tmp/os.snap.db"
    uv run python scripts/kb_graph_ab.py /tmp/os.snap.db [--window 1800] [--json]

Replays every `learn search` a work order ran, against arm A (`search_knowledge`, what the
worker actually got) and arm B (`kgraph.graph_search`, same row budget). The LABEL for a
search is the set of entries the same work order then opened with `learn show` within
`--window` seconds — the only relevance judgement the record holds. Searches with no such
show are replayed for displacement only.

The base is filtered to what existed when the query was asked (`ts <= search ts`), and the
expansion graph is built from the whole snapshot — an approximation the report states.
BM25 statistics are today's, not then's; that shifts both arms alike.

Takes a SNAPSHOT path, never the live database: `CentralStore` migrates what it opens.
No model is called.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from statistics import mean
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from jarvis.central_store import CentralStore, headline  # noqa: E402
from jarvis.kgraph import adjacency, graph_search, knowledge_edges  # noqa: E402

VARIANTS = [("A", None), ("B s3 r3", (3, 3)), ("B s3 r5", (3, 5)), ("B s5 r5", (5, 5))]


def labelled_searches(store: CentralStore, window: float) -> list[dict[str, Any]]:
    conn = store.conn
    searches = [dict(r) for r in conn.execute(
        "SELECT id, ts, wo_id, term FROM knowledge_reads"
        " WHERE verb='search' AND wo_id<>'' AND term<>'' ORDER BY ts")]
    shows = [dict(r) for r in conn.execute(
        "SELECT r.wo_id, r.ts, h.kn_id FROM knowledge_reads r"
        " JOIN knowledge_read_hits h ON h.read_id = r.id WHERE r.verb='show'")]
    for s in searches:
        s["labels"] = {x["kn_id"] for x in shows
                       if x["wo_id"] == s["wo_id"] and s["ts"] <= x["ts"] <= s["ts"] + window}
    return searches


def score(ranked: list[str], labels: set[str]) -> dict[str, float]:
    first = next((i for i, kid in enumerate(ranked) if kid in labels), None)
    return {"hit@5": float(first is not None and first < 5),
            "hit@20": float(first is not None),
            "recall": len(labels & set(ranked)) / len(labels),
            "mrr": 0.0 if first is None else 1 / (first + 1)}


def run(snapshot: Path, window: float) -> dict[str, Any]:
    store = CentralStore(snapshot)
    everything = store.search_knowledge("", limit=1_000_000)
    edges = knowledge_edges(everything)
    adj = adjacency(edges)
    created = {r["id"]: r["ts"] for r in everything}
    searches = labelled_searches(store, window)

    results: dict[str, list[dict[str, Any]]] = {name: [] for name, _ in VARIANTS}
    for s in searches:
        labels = {k for k in s["labels"] if created.get(k, 1e18) <= s["ts"]}
        visible = (lambda row, t=s["ts"]: row["ts"] <= t)
        for name, params in VARIANTS:
            seeds, reserve = params or (0, 0)
            rows = graph_search(store, s["term"], limit=20, seeds=seeds, reserve=reserve,
                                adj=adj, visible=visible)
            ids = [r["id"] for r in rows]
            rec = {"search": s["id"], "ids": ids, "labelled": bool(labels),
                   "via": sum(1 for r in rows if "via" in r),
                   "index_chars": sum(len(headline(r["content"])) for r in rows)}
            if labels:
                rec.update(score(ids, labels))
            results[name].append(rec)

    by_rel: dict[str, int] = {}
    for e in edges:
        by_rel[e.rel] = by_rel.get(e.rel, 0) + 1
    report: dict[str, Any] = {
        "snapshot": str(snapshot), "window_s": window, "entries": len(everything),
        "edges": by_rel, "searches": len(searches),
        "labelled": sum(1 for r in results["A"] if r["labelled"]), "arms": {}}
    base = {r["search"]: r for r in results["A"]}
    for name, recs in results.items():
        lab = [r for r in recs if r["labelled"]]
        arm = {k: round(mean(r[k] for r in lab), 3) for k in ("hit@5", "hit@20", "recall", "mrr")}
        arm["index_chars"] = round(mean(r["index_chars"] for r in recs))
        arm["searches_expanded"] = sum(1 for r in recs if r["via"])
        if name != "A":
            arm["rescued"] = sum(1 for r in lab if r["hit@20"] and not base[r["search"]]["hit@20"])
            arm["lost"] = sum(1 for r in lab if base[r["search"]]["hit@20"] and not r["hit@20"])
        report["arms"][name] = arm
    return report


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("snapshot", type=Path)
    ap.add_argument("--window", type=float, default=1800.0)
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    report = run(args.snapshot, args.window)
    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    print(f"{report['entries']} entries, edges {report['edges']}")
    print(f"{report['searches']} searches by work orders, {report['labelled']} labelled "
          f"(a `learn show` within {int(report['window_s'])}s)\n")
    cols = ["hit@5", "hit@20", "recall", "mrr", "index_chars", "searches_expanded",
            "rescued", "lost"]
    print(f"{'arm':10}" + "".join(f"{c:>18}" for c in cols))
    for name, arm in report["arms"].items():
        print(f"{name:10}" + "".join(f"{arm.get(c, '-')!s:>18}" for c in cols))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
