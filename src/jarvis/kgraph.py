"""The knowledge base as a graph: typed edges derived mechanically, and arm B of the A/B"""

from __future__ import annotations

import re
import sqlite3
import xml.etree.ElementTree as ET
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any

from .central_store import headline

KN_ID_RE = re.compile(r"\bkn-[0-9a-f]{8}\b")

#: Expansion order: the strongest claim that B matters to whoever found A comes first
REL_PRIORITY = {"supersedes": 0, "superseded_by": 0, "cites": 1, "cited_by": 2, "same_wo": 3}


@dataclass(frozen=True)
class Edge:
    src: str
    rel: str
    dst: str


def knowledge_edges(rows: Iterable[dict[str, Any]]) -> list[Edge]:
    """Every derivable edge between knowledge entries, each once"""
    rows = list(rows)
    ids = {r["id"] for r in rows}
    by_writer: dict[str, list[str]] = {}
    retired_by: dict[str, list[str]] = {}
    edges: set[Edge] = set()
    for r in rows:
        for cited in set(KN_ID_RE.findall(r["content"] or "")):
            if cited in ids and cited != r["id"]:
                edges.add(Edge(r["id"], "cites", cited))
        if r.get("wo_id"):
            by_writer.setdefault(r["wo_id"], []).append(r["id"])
        if r.get("retired_by_wo_id"):
            retired_by.setdefault(r["retired_by_wo_id"], []).append(r["id"])
    for wo, written in by_writer.items():
        for new in written:
            for old in retired_by.get(wo, ()):
                if new != old:
                    edges.add(Edge(new, "supersedes", old))
        for i, a in enumerate(sorted(written)):
            for b in sorted(written)[i + 1:]:
                edges.add(Edge(a, "same_wo", b))
    return sorted(edges, key=lambda e: (e.src, e.rel, e.dst))


def adjacency(edges: Iterable[Edge]) -> dict[str, list[tuple[str, str]]]:
    """id -> [(rel as seen from id, neighbour)], strongest relation first"""
    inverse = {"cites": "cited_by", "supersedes": "superseded_by", "same_wo": "same_wo"}
    adj: dict[str, list[tuple[str, str]]] = {}
    for e in edges:
        adj.setdefault(e.src, []).append((e.rel, e.dst))
        adj.setdefault(e.dst, []).append((inverse[e.rel], e.src))
    for nbrs in adj.values():
        nbrs.sort(key=lambda rn: (REL_PRIORITY[rn[0]], rn[1]))
    return adj


def graph_search(store: Any, term: str, limit: int = 20, project: str | None = None,
                 topic: str | None = None, *, seeds: int = 3, reserve: int = 5,
                 adj: dict[str, list[tuple[str, str]]] | None = None,
                 visible: Callable[[dict[str, Any]], bool] | None = None,
                 ) -> list[dict[str, Any]]:
    """Arm B: BM25 hits, with up to `reserve` slots given to neighbours of the top `seeds`"""
    ok = visible or (lambda _row: True)
    pool = store.search_knowledge(term, limit=limit * 10 if visible else limit,
                                  project=project, topic=topic)
    hits = [r for r in pool if ok(r)][:limit]
    if adj is None:
        adj = adjacency(knowledge_edges(store.search_knowledge("", limit=1_000_000)))
    have = {r["id"] for r in hits}
    extra: list[dict[str, Any]] = []
    for seed in hits[:seeds]:
        for rel, nid in adj.get(seed["id"], ()):
            if len(extra) >= reserve:
                break
            if nid in have:
                continue
            row = store.get_knowledge(nid)
            if row is None or not ok(row):
                continue
            if project is not None and row["project"] not in ("", project):
                continue
            if topic is not None and row["topic"] != topic:
                continue
            have.add(nid)
            extra.append({**row, "via": f"{rel} {seed['id']}"})
    keep = max(min(len(hits), limit - len(extra)), 0)
    return hits[:keep] + extra


# -- the review tool's payload


def review_payload(store: Any, include_retired: bool = False) -> dict[str, Any]:
    """Everything the knowledge-review page draws, as one JSON-ready dict"""
    rows = store.search_knowledge("", limit=1_000_000)
    assigned = store.domain_assignments(with_by=True)
    reads = store.knowledge_hit_counts()
    nodes = []
    for r in rows:
        if r["retired_at"] is not None and not include_retired:
            continue
        domain_id, by = assigned.get(r["id"], ("", ""))
        nodes.append({"id": r["id"], "headline": headline(r["content"]),
                      "topic": r["topic"], "project": r["project"] or "global",
                      "retired": r["retired_at"] is not None, "reads": reads.get(r["id"], 0),
                      "chars": len(r["content"] or ""), "domain_id": domain_id,
                      "assigned_by": by, "wo_id": r["wo_id"]})
    shown = {n["id"] for n in nodes}
    edges = [{"src": e.src, "rel": e.rel, "dst": e.dst, "manual": False}
             for e in knowledge_edges(rows) if e.src in shown and e.dst in shown]
    edges += [{"id": lk["id"], "src": lk["src"], "rel": lk["rel"], "dst": lk["dst"],
               "manual": True, "by": lk["by"]}
              for lk in store.list_kb_links() if lk["src"] in shown and lk["dst"] in shown]
    domains = [{k: d[k] for k in ("id", "name", "description", "parent_id", "by")}
               for d in store.list_domains()]
    return {"nodes": nodes, "domains": domains, "edges": edges}


# -- the picture


def graph_nodes_edges(central: sqlite3.Connection, neo: sqlite3.Connection | None = None,
                      reads: dict[str, int] | None = None,
                      ) -> tuple[list[dict[str, Any]], list[tuple[str, str, str]]]:
    """Nodes and edges for the GEXF export: knowledge entries, plus Neo learnings when neo.db is given"""
    central.row_factory = sqlite3.Row
    reads = reads or {}
    nodes: dict[str, dict[str, Any]] = {}
    edges: list[tuple[str, str, str]] = []

    def wo(wo_id: str) -> str:
        nodes.setdefault(wo_id, {"id": wo_id, "label": wo_id, "kind": "work_order"})
        return wo_id

    kn = [dict(r) for r in central.execute("SELECT * FROM knowledge")]
    for r in kn:
        nodes[r["id"]] = {"id": r["id"], "label": headline(r["content"]),
                          "kind": "knowledge", "topic": r["topic"],
                          "project": r["project"] or "global",
                          "retired": r["retired_at"] is not None,
                          "reads": reads.get(r["id"], 0), "ts": r["ts"]}
        if r["wo_id"]:
            edges.append((wo(r["wo_id"]), "wrote", r["id"]))
        if r["retired_by_wo_id"]:
            edges.append((wo(r["retired_by_wo_id"]), "retired", r["id"]))
    edges += [(e.src, e.rel, e.dst) for e in knowledge_edges(kn) if e.rel != "same_wo"]

    for r in central.execute("SELECT * FROM gate_rules WHERE source <> 'builtin'"):
        gid = f"gate-{r['id']}"
        nodes[gid] = {"id": gid, "label": r["summary"] or r["pattern"], "kind": "gate_rule",
                      "project": r["project"] or "global",
                      "retired": r["retired_at"] is not None, "ts": r["ts"]}
        if r["wo_id"]:
            edges.append((wo(r["wo_id"]), "learned", gid))

    if neo is not None:
        neo.row_factory = sqlite3.Row
        for r in neo.execute(
                "SELECT l.*, q.wo_id AS q_wo FROM learnings l"
                " LEFT JOIN questions q ON q.id = l.question_id"):
            nid = f"neo-{r['id']}"
            nodes[nid] = {"id": nid, "label": headline(r["content"]),
                          "kind": "neo_learning", "project": r["project"] or "global",
                          "retired": r["retired_at"] is not None, "ts": r["ts"]}
            if r["q_wo"]:
                edges.append((wo(r["q_wo"]), "taught", nid))
    return list(nodes.values()), edges


def to_gexf(nodes: list[dict[str, Any]], edges: list[tuple[str, str, str]]) -> bytes:
    """GEXF 1.3 — Gephi Lite and desktop Gephi open it as is"""
    attrs = ["kind", "topic", "project", "retired", "reads"]
    types = {"retired": "boolean", "reads": "integer"}
    root = ET.Element("gexf", xmlns="http://gexf.net/1.3", version="1.3")
    graph = ET.SubElement(root, "graph", defaultedgetype="directed")
    decl = ET.SubElement(graph, "attributes", {"class": "node"})
    for i, a in enumerate(attrs):
        ET.SubElement(decl, "attribute", id=str(i), title=a, type=types.get(a, "string"))
    edecl = ET.SubElement(graph, "attributes", {"class": "edge"})
    ET.SubElement(edecl, "attribute", id="0", title="rel", type="string")
    xnodes = ET.SubElement(graph, "nodes")
    for n in nodes:
        el = ET.SubElement(xnodes, "node", id=n["id"], label=n.get("label") or n["id"])
        vals = ET.SubElement(el, "attvalues")
        for i, a in enumerate(attrs):
            if a in n:
                v = n[a]
                ET.SubElement(vals, "attvalue", {
                    "for": str(i), "value": str(v).lower() if isinstance(v, bool) else str(v)})
    xedges = ET.SubElement(graph, "edges")
    for i, (src, rel, dst) in enumerate(edges):
        el = ET.SubElement(xedges, "edge", id=str(i), source=src, target=dst, label=rel)
        ET.SubElement(ET.SubElement(el, "attvalues"), "attvalue", {"for": "0", "value": rel})
    return ET.tostring(root, encoding="utf-8", xml_declaration=True)

