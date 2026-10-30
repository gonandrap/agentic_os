"""The knowledge-graph prototype: mechanical edges, arm B's row budget, the GEXF export"""

from __future__ import annotations

import xml.etree.ElementTree as ET

from jarvis.central_store import CentralStore
from jarvis.cli import build_parser, cmd_learn
from jarvis.kgraph import (
    Edge,
    adjacency,
    graph_nodes_edges,
    graph_search,
    knowledge_edges,
    to_gexf,
)


def _base(central: CentralStore) -> dict[str, str]:
    old = central.add_knowledge("deploy uses rsync", topic="deploy", wo_id="wo-a")["id"]
    new = central.add_knowledge(f"deploy uses the release script (replaces {old})",
                                topic="deploy", wo_id="wo-b")["id"]
    sib = central.add_knowledge("release script needs a clean tree", topic="ops",
                                wo_id="wo-b")["id"]
    central.retract_knowledge(old, "release script replaced rsync", wo_id="wo-b")
    lone = central.add_knowledge("unrelated cache note", topic="cache")["id"]
    return {"old": old, "new": new, "sib": sib, "lone": lone}


def test_edges_are_read_off_text_and_provenance(jarvis_home):
    central = CentralStore()
    ids = _base(central)
    edges = set(knowledge_edges(central.search_knowledge("", limit=100)))
    assert Edge(ids["new"], "cites", ids["old"]) in edges
    assert Edge(ids["new"], "supersedes", ids["old"]) in edges
    assert Edge(ids["sib"], "supersedes", ids["old"]) in edges
    a, b = sorted([ids["new"], ids["sib"]])
    assert Edge(a, "same_wo", b) in edges
    assert not any(ids["lone"] in (e.src, e.dst) for e in edges)


def test_arm_b_spends_the_same_row_budget_and_says_why(jarvis_home):
    central = CentralStore()
    ids = _base(central)
    for i in range(30):
        central.add_knowledge(f"deploy padding {i}", topic="deploy")
    adj = adjacency(knowledge_edges(central.search_knowledge("", limit=100)))
    plain = central.search_knowledge("rsync", limit=5)
    rows = graph_search(central, "rsync", limit=5, seeds=1, reserve=2, adj=adj)
    assert len(rows) <= 5
    assert rows[0]["id"] == plain[0]["id"] == ids["old"]
    vias = {r["id"]: r["via"] for r in rows if "via" in r}
    assert vias and all(v.endswith(ids["old"]) for v in vias.values())
    assert set(vias) <= {ids["new"], ids["sib"]}


def test_arm_b_with_no_reserve_is_arm_a(jarvis_home):
    central = CentralStore()
    _base(central)
    plain = [r["id"] for r in central.search_knowledge("deploy", limit=20)]
    assert [r["id"] for r in graph_search(central, "deploy", reserve=0)] == plain


def test_gexf_parses_and_carries_every_node_and_edge(jarvis_home):
    central = CentralStore()
    ids = _base(central)
    nodes, edges = graph_nodes_edges(central.conn)
    root = ET.fromstring(to_gexf(nodes, edges))
    ns = {"g": "http://gexf.net/1.3"}
    xnodes = {n.get("id") for n in root.iterfind(".//g:node", ns)}
    assert set(ids.values()) | {"wo-a", "wo-b"} <= xnodes
    rels = {e.get("label") for e in root.iterfind(".//g:edge", ns)}
    assert {"wrote", "retired", "cites", "supersedes"} <= rels
    assert len(root.findall(".//g:edge", ns)) == len(edges)


def test_learn_graph_writes_the_file(jarvis_home, tmp_path, capsys):
    _base(CentralStore())
    out = tmp_path / "kb.gexf"
    assert cmd_learn(build_parser().parse_args(["learn", "graph", "-o", str(out)])) == 0
    assert out.read_bytes().startswith(b"<?xml")
    assert "nodes" in capsys.readouterr().out
