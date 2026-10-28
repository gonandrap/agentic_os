"""A deterministic first proposal of knowledge DOMAINS, for a person to review.

PROTOTYPE. 122 free-text topics over ~800 entries (median two entries a topic, 52 of one)
are too many to browse and too few per bucket to be worth opening. This groups topics
into a handful of domains with no model call, so the proposal costs nothing and runs the
same way twice:

1. A topic's TOKENS: its name lower-cased, split on non-letters, stop words dropped,
   plurals folded (`privileged-action-gates`, `ui gates` and `gates neo` share `gate`).
2. Topic AFFINITY: entry-to-entry edges between the two topics (`kgraph.knowledge_edges`:
   cites, supersedes, same_wo), normalised by the smaller topic, plus a bonus for shared
   tokens.
3. Greedy agglomeration: merge the most-affine pair of groups until `target` groups
   remain or nothing affine is left, never past `max_share` of the base in one group.
4. An entry with no topic joins the group most of its edges point into; else stays
   unassigned.

A group's NAME is its largest topic and its description lists its biggest topics. The
proposal is written with actor `process:kb-regroup`; every assignment a person makes in
the review tool supersedes it as `human:…`.
"""

from __future__ import annotations

import re
from collections import Counter
from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any

from .kgraph import Edge, knowledge_edges

ACTOR = "process:kb-regroup"
AFFINITY_RELS = ("cites", "supersedes")
STOP = {"and", "the", "of", "a", "an", "os", "to", "in", "on", "for", "vs", "its", "is",
        "it", "un", "two", "one", "shared", "fast", "path"}


def tokens(topic: str) -> frozenset[str]:
    out = set()
    for w in re.split(r"[^a-z]+", topic.lower()):
        if len(w) < 3 or w in STOP:
            continue
        out.add(w[:-1] if w.endswith("s") and not w.endswith("ss") and len(w) > 4 else w)
    return frozenset(out)


@dataclass
class Group:
    topics: list[str]
    size: int
    toks: Counter[str] = field(default_factory=Counter)


def affinity(a: Group, b: Group, links: dict[tuple[str, str], int],
             token_bonus: float) -> float:
    n = sum(links.get((s, t), 0) + links.get((t, s), 0) for s in a.topics for t in b.topics)
    shared = len(a.toks.keys() & b.toks.keys())
    # Cosine-style: dividing by the SMALLER group let big groups swallow every neighbour
    return n / max((a.size * b.size) ** 0.5, 1) + token_bonus * shared


def propose(rows: Sequence[dict[str, Any]], *, target: int = 12, max_share: float = 0.15,
            token_bonus: float = 0.5, min_size: int = 8,
            edges: Iterable[Edge] | None = None) -> dict[str, Any]:
    """{"domains": [{name, description, topics, size}], "assign": {kn_id: domain index}}"""
    live = [r for r in rows if r.get("retired_at") is None]
    topic_of = {r["id"]: r["topic"] for r in live}
    sizes = Counter(r["topic"] for r in live if r["topic"])
    edges = list(knowledge_edges(live) if edges is None else edges)
    links: Counter[tuple[str, str]] = Counter()
    for e in edges:
        ts, td = topic_of.get(e.src, ""), topic_of.get(e.dst, "")
        # same_wo is not affinity: one work order writes about many unrelated things
        if ts and td and ts != td and e.rel in AFFINITY_RELS:
            links[(ts, td)] += 1

    groups = [Group([t], n, Counter(tokens(t))) for t, n in
              sorted(sizes.items(), key=lambda kv: (-kv[1], kv[0]))]
    cap = max_share * len(live)
    while len(groups) > target:
        best: tuple[float, int, int] | None = None
        for i, a in enumerate(groups):
            for j in range(i + 1, len(groups)):
                b = groups[j]
                if a.size + b.size > cap:
                    continue
                score = affinity(a, b, links, token_bonus)
                if score > 0 and (best is None or score > best[0]):
                    best = (score, i, j)
        if best is None:
            break
        _, i, j = best
        a, b = groups[i], groups[j]
        groups[i] = Group(a.topics + b.topics, a.size + b.size, a.toks + b.toks)
        del groups[j]

    # A group still under `min_size` folds into the group it is most affine to; with no
    # affinity at all it folds into one "unfiled" group a person sorts by hand.
    groups.sort(key=lambda g: (-g.size, g.topics[0]))
    big = [g for g in groups if g.size >= min_size]
    unfiled = Group([], 0)
    for g in (g for g in groups if g.size < min_size):
        # Highest affinity wins; a tie goes to the earlier (larger) group
        score, home = max(((affinity(g, b, links, token_bonus), -bi)
                           for bi, b in enumerate(big)), default=(0.0, 0))
        if score > 0:
            b = big[-home]
            b.topics += g.topics
            b.size += g.size
            b.toks += g.toks
        else:
            unfiled.topics += g.topics
            unfiled.size += g.size
    groups = big + ([unfiled] if unfiled.topics else [])
    groups.sort(key=lambda g: (g is unfiled, -g.size, g.topics[0]))
    topic_group = {t: gi for gi, g in enumerate(groups) for t in g.topics}
    assign = {k: topic_group[t] for k, t in topic_of.items() if t in topic_group}

    adj: dict[str, Counter[int]] = {}
    for e in edges:
        for x, y in ((e.src, e.dst), (e.dst, e.src)):
            if y in assign:
                adj.setdefault(x, Counter())[assign[y]] += 1
    for k, t in topic_of.items():
        if not t and k in adj:
            assign[k] = min(adj[k].items(), key=lambda kv: (-kv[1], kv[0]))[0]

    domains = []
    for gi, g in enumerate(groups):
        by_size = sorted(g.topics, key=lambda t: (-sizes[t], t))
        domains.append({"name": "unfiled" if g is unfiled else by_size[0], "topics": by_size,
                        "size": sum(1 for v in assign.values() if v == gi),
                        "description": ", ".join(by_size[:4])
                        + (f" and {len(by_size) - 4} more" if len(by_size) > 4 else "")})
    return {"domains": domains, "assign": assign}


def apply(store: Any, proposal: dict[str, Any], *, by: str = ACTOR) -> dict[str, str]:
    """Write the proposal as domains + assignments; returns domain index -> domain id"""
    ids: dict[int, str] = {}
    for i, d in enumerate(proposal["domains"]):
        name, n = d["name"], 2
        while store._live_domain_named(name, ""):
            name, n = f"{d['name']} ({n})", n + 1
        ids[i] = store.add_domain(name, description=d["description"], by=by)["id"]
    by_domain: dict[int, list[str]] = {}
    for kn, gi in proposal["assign"].items():
        by_domain.setdefault(gi, []).append(kn)
    for gi, kns in by_domain.items():
        store.assign_domain(sorted(kns), ids[gi], by=by)
    return {str(k): v for k, v in ids.items()}
