#!/usr/bin/env python3
"""Propose knowledge domains (no model), and measure what they would change.

    cp $SNAP <dir>/os.db
    uv run python scripts/kb_regroup.py <dir>/os.db [--target 12] [--apply] [--json]

Prints the proposal, then two measurements against today's prompt and search:

* INDEX SIZE — the knowledge block a `jarvis_os` dispatch carries today
  (`dispatch.render_knowledge_block`, default config, no title hints) against the same
  block with the headline index + topic roll-call replaced by one line per domain.
* BROWSE REACH — for each labelled `learn search` that missed (see kb_graph_ab.py), is the
  entry the worker went on to open in the DOMAIN of one of the top-5 hits, and how many
  entries would browsing that domain mean.

`--apply` writes the proposal into the given database as `process:kb-regroup`, for the
review tool. Takes a COPY, never the live database. No model is called.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from statistics import median
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from jarvis import kb_regroup  # noqa: E402
from jarvis.catalog import OsConfig  # noqa: E402
from jarvis.central_store import CentralStore  # noqa: E402
from jarvis.dispatch import render_knowledge_block  # noqa: E402
from kb_graph_ab import labelled_searches  # noqa: E402

PROJECT = "jarvis_os"


def index_chars(store: CentralStore, proposal: dict[str, Any]) -> dict[str, int]:
    cfg = OsConfig()
    brief = store.knowledge_brief(
        PROJECT, pinned_limit=cfg.knowledge_inject_limit,
        digest_limit=cfg.knowledge_digest_limit, digest_chars=cfg.knowledge_digest_chars,
        hint_limit=0, pinned_chars=cfg.knowledge_pinned_chars)
    today = len("\n".join(render_knowledge_block(brief, PROJECT)))
    brief.digest, brief.overflow = [], []
    domains = ["", "## Domains — `jarvis learn list --domain <name>` for headlines"]
    domains += [f"- {d['name']} ({d['size']}) — {d['description']}"
                for d in proposal["domains"] if d["size"]]
    with_domains = len("\n".join(render_knowledge_block(brief, PROJECT) + domains))
    return {"today": today, "with_domains": with_domains}


def browse_reach(store: CentralStore, proposal: dict[str, Any]) -> dict[str, Any]:
    rows = {r["id"]: r for r in store.search_knowledge("", limit=1_000_000)}
    dom = proposal["assign"]
    sizes = {i: d["size"] for i, d in enumerate(proposal["domains"])}
    misses, reach, browse = 0, 0, []
    for s in labelled_searches(store, 1800):
        labels = {k for k in s["labels"] if k in rows and rows[k]["ts"] <= s["ts"]}
        if not labels:
            continue
        pool = [r for r in store.search_knowledge(s["term"], limit=1000)
                if r["ts"] <= s["ts"]]
        if labels & {r["id"] for r in pool[:20]}:
            continue
        misses += 1
        near = {dom[r["id"]] for r in pool[:5] if r["id"] in dom}
        hit = near & {dom[k] for k in labels if k in dom}
        if hit:
            reach += 1
            browse.append(min(sizes[d] for d in hit))
    return {"misses": misses, "reachable_by_domain": reach,
            "median_domain_size_to_browse": median(browse) if browse else None}


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=(__doc__ or "").split("\n")[0])
    ap.add_argument("db", type=Path)
    ap.add_argument("--target", type=int, default=12)
    ap.add_argument("--max-share", type=float, default=0.15)
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)
    store = CentralStore(args.db)
    rows = store.search_knowledge("", limit=1_000_000)
    proposal = kb_regroup.propose(rows, target=args.target, max_share=args.max_share)
    report = {"domains": proposal["domains"],
              "assigned": len(proposal["assign"]),
              "live": sum(1 for r in rows if r["retired_at"] is None),
              "index_chars": index_chars(store, proposal),
              "browse": browse_reach(store, proposal)}
    if args.apply:
        report["applied"] = kb_regroup.apply(store, proposal)
    if args.json:
        print(json.dumps(report, indent=2))
        return 0
    print(f"{report['assigned']}/{report['live']} live entries assigned to "
          f"{len(proposal['domains'])} domains\n")
    for d in proposal["domains"]:
        print(f"  {d['size']:4}  {d['name']:32} {len(d['topics']):3} topics: "
              f"{', '.join(d['topics'][:8])}{' …' if len(d['topics']) > 8 else ''}")
    ic, br = report["index_chars"], report["browse"]
    print(f"\nindex block ({PROJECT}): {ic['today']} chars today -> "
          f"{ic['with_domains']} with domains")
    print(f"search misses reachable by browsing a top-5 hit's domain: "
          f"{br['reachable_by_domain']}/{br['misses']} "
          f"(median domain size {br['median_domain_size_to_browse']})")
    if args.apply:
        print(f"applied: {len(report['applied'])} domains written as {kb_regroup.ACTOR}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
