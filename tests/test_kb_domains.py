"""Knowledge domains, assignments and manual links: append-only, refused when malformed"""

from __future__ import annotations

import pytest

from jarvis import ops
from jarvis.central_store import CentralStore
from jarvis.ops import OpsError


def test_a_newer_assignment_supersedes_and_none_is_erased(jarvis_home):
    c = CentralStore()
    kn = c.add_knowledge("a")["id"]
    d1 = c.add_domain("gates", by="process:kb-regroup")["id"]
    d2 = c.add_domain("spend")["id"]
    c.assign_domain([kn], d1, by="process:kb-regroup")
    c.assign_domain([kn], d2, by="human:user")
    assert c.domain_assignments(with_by=True)[kn] == (d2, "human:user")
    assert c.conn.execute("SELECT COUNT(*) FROM kb_assignments").fetchone()[0] == 2


def test_retiring_a_domain_moves_its_members_and_children_up(jarvis_home):
    c = CentralStore()
    kn = c.add_knowledge("a")["id"]
    top = c.add_domain("orders")["id"]
    mid = c.add_domain("reviews", parent_id=top)["id"]
    leaf = c.add_domain("assumptions", parent_id=mid)["id"]
    c.assign_domain([kn], mid)
    c.retire_domain(mid, "too fine-grained")
    assert c.domain_assignments()[kn] == top
    assert c.get_domain(leaf)["parent_id"] == top
    assert mid not in {d["id"] for d in c.list_domains()}
    with pytest.raises(ValueError):
        c.retire_domain(top, " ")


def test_domain_tree_refuses_cycles_duplicates_and_dead_parents(jarvis_home):
    c = CentralStore()
    a = c.add_domain("a")["id"]
    b = c.add_domain("b", parent_id=a)["id"]
    with pytest.raises(ValueError):
        c.update_domain(a, parent_id=b)
    with pytest.raises(ValueError):
        c.add_domain("b", parent_id=a)
    c.add_domain("b")  # same name, different parent: fine
    c.retire_domain(b, "gone")
    with pytest.raises(KeyError):
        c.add_domain("x", parent_id=b)


def test_links_are_validated_and_retired_with_a_reason(jarvis_home):
    c = CentralStore()
    a, b = c.add_knowledge("a")["id"], c.add_knowledge("b")["id"]
    with pytest.raises(ValueError):
        c.add_kb_link(a, "likes", b)
    with pytest.raises(KeyError):
        c.add_kb_link(a, "relates_to", "kn-00000000")
    link = c.add_kb_link(a, "duplicates", b, by="human:user")
    c.retire_kb_link(link["id"], "not a duplicate after all")
    assert c.list_kb_links() == []
    assert c.list_kb_links(include_retired=True)[0]["retired_reason"]


def test_review_payload_carries_assignment_actor_and_both_edge_kinds(jarvis_home):
    c = CentralStore()
    a = c.add_knowledge("first", wo_id="wo-1")["id"]
    b = c.add_knowledge(f"second, see {a}", wo_id="wo-1")["id"]
    d = ops.kb_domain_add("gates", by="process:kb-regroup")["id"]
    ops.kb_assign([a], d, by="process:kb-regroup")
    ops.kb_link_add(b, "depends_on", a)
    p = ops.kb_review_payload()
    node = {n["id"]: n for n in p["nodes"]}
    assert node[a]["domain_id"] == d and node[a]["assigned_by"] == "process:kb-regroup"
    assert node[b]["domain_id"] == ""
    assert {(e["rel"], e["manual"]) for e in p["edges"]} >= {
        ("cites", False), ("same_wo", False), ("depends_on", True)}
    assert [x["name"] for x in p["domains"]] == ["gates"]


def test_ops_turns_store_refusals_into_ops_errors(jarvis_home):
    with pytest.raises(OpsError):
        ops.kb_assign(["kn-00000000"], "")
    with pytest.raises(OpsError):
        ops.kb_domain_add("  ")
