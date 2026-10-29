"""Fleet-wide artifact search: one query over every kind of record the OS keeps (docs/superpowers/specs/2026-09-23-artifact-search.md)"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import db

if TYPE_CHECKING:
    from .project_store import ProjectStore

# Every kind a search can return, in the order a tie is broken
KINDS = (
    "work_order",
    "feature_order",
    "neo_question",
    "alarm",
    "gate",
    "backlog",
    "knowledge",
)

SNIPPET_CHARS = 180

# An id typed in full is a jump, not a search: it outranks everything else whatever the rest of the...
EXACT_ID_BONUS = 100


def _snippet(text: str, words: list[str], limit: int = SNIPPET_CHARS) -> str:
    """The matched part of a body, not its first line: a hit the user cannot see the reason for is a row..."""
    body = " ".join((text or "").split())
    if not body:
        return ""
    low = body.lower()
    at = min((low.find(w) for w in words if w in low), default=-1)
    if at < 0 or at < limit:
        return body[:limit] + ("…" if len(body) > limit else "")
    start = max(0, at - limit // 3)
    return "…" + body[start:start + limit] + ("…" if start + limit < len(body) else "")


def _hit(kind: str, *, id: str, project: str, title: str, status: str, ts: float,
         score: float, body: str, words: list[str], url: str,
         ref: str) -> dict[str, Any]:
    snippet = _snippet(body, words)
    # A knowledge entry's title IS the head of its body
    if snippet[:40] == title[:40]:
        snippet = ""
    return {
        "kind": kind, "id": id, "project": project, "title": title, "status": status,
        "ts": ts or 0.0, "score": float(score), "snippet": snippet,
        "url": url, "ref": ref,
    }


def _project_paths(project: str | None) -> dict[str, Path]:
    from . import ops  # ops imports the stores; the cycle is only ever this way round
    # Archived history stays findable (docs/superpowers/specs/2026-10-28-offboard-a-project.md §2)
    paths = ops.registered_project_paths(include_archived=True)
    if project:
        if project not in paths:
            raise ops.OpsError(f"project {project!r} not registered "
                               f"(known: {sorted(paths)})")
        paths = {project: paths[project]}
    return paths


def _from_project(store: "ProjectStore", name: str, words: list[str], kinds: set[str],
                  limit: int) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    if "work_order" in kinds:
        for row in store.search_work_orders(words, limit=limit):
            out.append(_hit("work_order", id=row["id"], project=name,
                            title=row["title"], status=row["status"],
                            ts=row["created_at"], score=row["_score"],
                            body=row.get("result_summary") or row["description"],
                            words=words, url=f"/wo/{name}/{row['id']}",
                            ref=f"jarvis wo show {row['id']}"))
    if "feature_order" in kinds:
        # Issue #997: every kind printed `/fo/` and `jarvis fo show`
        from . import ops
        from .project_store import order_path
        for row in store.search_feature_orders(words, limit=limit):
            out.append(_hit("feature_order", id=row["id"], project=name,
                            title=row["title"], status=row["status"],
                            ts=row["created_at"], score=row["_score"],
                            body=row["description"], words=words,
                            url=order_path(name, row["id"]),
                            ref=f"{ops.show_verb(row.get('kind'))} {row['id']}"))
    if "alarm" in kinds:
        for row in store.search_alarms(words, limit=limit):
            out.append(_hit("alarm", id=row["id"], project=name,
                            title=f"{row['kind']} on {row['wo_title']}",
                            status=row["status"], ts=row["ts"], score=row["_score"],
                            body=row.get("note") or row["reason"], words=words,
                            url=f"/alarms/{name}/{row['id']}",
                            ref=f"jarvis alarms show {row['id']}"))
    if "gate" in kinds:
        for row in store.search_gates(words, limit=limit):
            out.append(_hit("gate", id=str(row["id"]), project=name,
                            title=f"{row['kind']} for {row['wo_id']}",
                            status=row["status"], ts=row["ts"], score=row["_score"],
                            body=row["command"], words=words,
                            url=f"/gates/{row['id']}?project={name}",
                            ref=f"jarvis gate show {row['id']}"))
    return out


def search(query: str, *, project: str | None = None,
           kinds: tuple[str, ...] | None = None,
           limit: int = 30) -> list[dict[str, Any]]:
    """Every artifact matching `query`, most relevant first"""
    # Imported here, not at module level: `build_parser` reads KINDS on EVERY cli invocation
    from .central_store import CentralStore, headline
    from .neo_store import NeoStore
    from .project_store import ProjectStore

    words = db.search_words(query)
    if not words:
        return []
    wanted = set(kinds or KINDS)
    unknown = wanted - set(KINDS)
    if unknown:
        raise ValueError(f"unknown kind(s): {sorted(unknown)} (known: {list(KINDS)})")

    from . import ops
    archived = (set(ops.registered_project_paths(include_archived=True))
                - set(ops.registered_project_paths()))

    hits: list[dict[str, Any]] = []
    if wanted & {"work_order", "feature_order", "alarm", "gate"}:
        for name, path in _project_paths(project).items():
            if not path.is_dir():
                continue
            store = ProjectStore(path)
            try:
                hits += _from_project(store, name, words, wanted, limit)
            finally:
                store.close()

    if wanted & {"backlog", "knowledge"}:
        central = CentralStore()
        try:
            if "backlog" in wanted:
                for row in central.search_backlog(words, project=project, limit=limit):
                    hits.append(_hit("backlog", id=row["id"], project=row["project"],
                                     title=row["title"], status=row["status"],
                                     ts=row["created_at"], score=row["_score"],
                                     body=row["description"], words=words,
                                     url=f"/backlog/{row['id']}",
                                     ref=f"jarvis backlog show {row['id']}"))
            if "knowledge" in wanted:
                rows = central.search_knowledge(query, limit=limit, project=project)
                for rank, row in enumerate(rows):
                    hits.append(_hit("knowledge", id=row["id"], project=row["project"],
                                     title=headline(row["content"]),
                                     status="retired" if row["retired_at"] else "",
                                     ts=row["ts"], score=len(words) - rank / len(rows),
                                     body=row["content"], words=words,
                                     url=f"/knowledge/{row['id']}",
                                     ref=f"jarvis learn show {row['id']}"))
        finally:
            central.close()

    if "neo_question" in wanted:
        neo = NeoStore()
        try:
            for row in neo.search_questions(words, project=project or "", limit=limit):
                hits.append(_hit("neo_question", id=str(row["id"]),
                                 project=row["project"], title=row["question"],
                                 status=row["status"], ts=row["ts"],
                                 score=row["_score"],
                                 body=row.get("answer") or row["question"], words=words,
                                 url=f"/neo/question/{row['id']}",
                                 ref=f"jarvis neo show {row['id']}"))
        finally:
            neo.close()

    exact = query.strip().lower()
    for hit in hits:
        # Readable record, project not running
        hit["archived"] = hit["project"] in archived
        if hit["id"].lower() == exact:
            hit["score"] += EXACT_ID_BONUS
    hits.sort(key=lambda h: (-h["score"], KINDS.index(h["kind"]), -h["ts"]))
    return hits[:limit]


def counts(hits: list[dict[str, Any]]) -> list[tuple[str, int]]:
    """How many hits of each kind, in KINDS order — the line a listing leads with"""
    return [(k, sum(1 for h in hits if h["kind"] == k)) for k in KINDS
            if any(h["kind"] == k for h in hits)]
