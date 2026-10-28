"""The deterministic domain proposal: grouping, folding, and what `apply` writes"""

from __future__ import annotations

from jarvis import kb_regroup
from jarvis.central_store import CentralStore


def _rows():
    rows, n = [], 0

    def add(topic, content=""):
        nonlocal n
        n += 1
        kid = f"kn-{n:08x}"
        rows.append({"id": kid, "topic": topic, "content": content, "retired_at": None,
                     "wo_id": "", "retired_by_wo_id": ""})
        return kid

    gates = [add("gates") for _ in range(6)]
    for _ in range(3):
        add("privileged-action-gates", f"see {gates[0]}")  # tokens AND a citation
    cost = [add("token economics") for _ in range(6)]
    for _ in range(3):
        add("cost accounting", f"see {cost[0]}")
    add("onboarding")  # no affinity to anything: unfiled
    add("", f"untopiced, cites {gates[1]} and {gates[2]}")  # follows its edges
    return rows


def test_topics_group_by_tokens_and_citations_and_orphans_are_unfiled():
    p = kb_regroup.propose(_rows(), target=2, min_size=3, max_share=0.6)
    names = [d["name"] for d in p["domains"]]
    assert names[-1] == "unfiled" and p["domains"][-1]["topics"] == ["onboarding"]
    by_topic = {t: d["name"] for d in p["domains"] for t in d["topics"]}
    assert by_topic["privileged-action-gates"] == by_topic["gates"] == "gates"
    assert by_topic["cost accounting"] == by_topic["token economics"]
    untopiced = next(r["id"] for r in _rows() if not r["topic"])
    assert p["domains"][p["assign"][untopiced]]["name"] == "gates"


def test_the_proposal_is_deterministic():
    assert kb_regroup.propose(_rows(), target=2, min_size=3, max_share=0.6) == \
        kb_regroup.propose(list(reversed(_rows())), target=2, min_size=3, max_share=0.6)


def test_the_size_cap_keeps_groups_apart():
    p = kb_regroup.propose(_rows(), target=1, max_share=0.5, min_size=1)
    assert max(d["size"] for d in p["domains"]) <= 0.5 * 20 + 1


def test_apply_writes_domains_and_assignments_as_the_process_actor(jarvis_home):
    c = CentralStore()
    a = c.add_knowledge("one", topic="gates")["id"]
    b = c.add_knowledge(f"two, see {a}", topic="privileged action gates")["id"]
    c.add_domain("gates")  # a human-made domain of the same name already exists
    p = kb_regroup.propose(c.search_knowledge("", limit=100), target=1, min_size=1, max_share=1.0)
    kb_regroup.apply(c, p)
    got = c.domain_assignments(with_by=True)
    assert got[a][1] == got[b][1] == kb_regroup.ACTOR and got[a][0] == got[b][0]
    assert c.get_domain(got[a][0])["name"] == "gates (2)"
