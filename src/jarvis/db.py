"""SQLite helpers shared by the central and per-project stores."""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any


def connect(path: Path) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=10, isolation_level=None)  # autocommit
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


@contextmanager
def write_transaction(conn: sqlite3.Connection) -> Iterator[sqlite3.Connection]:
    """One all-or-nothing unit of work. THE ONLY WAY TO OPEN A TRANSACTION HERE.

    `BEGIN IMMEDIATE`, never a bare `BEGIN`: a deferred transaction that reads before it
    writes takes a WAL snapshot, and the upgrade to a write returns SQLITE_BUSY_SNAPSHOT
    — reported as `database is locked` — the moment any other connection committed after
    that snapshot. SQLite does not consult `busy_timeout` for that code, so the ten
    seconds set in `connect` never applies and the caller fails instantly. Taking the
    write lock up front turns that into an ordinary wait the busy handler can serve.
    The daemon writes to one project database from several threads, so this is not
    theoretical: tests/test_stores.py reproduces it.
    """
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def now() -> float:
    return time.time()


def new_id(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def to_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False)


def from_json(value: str | None, default: Any = None) -> Any:
    if not value:
        return default
    try:
        return json.loads(value)
    except json.JSONDecodeError:
        return default


def rows_to_dicts(rows: list[sqlite3.Row]) -> list[dict[str, Any]]:
    return [dict(r) for r in rows]


# Substring matching makes a one- or two-letter word match almost every row ("in" is
# inside "login"), so a query's short words are dropped — unless that is all it has, and
# then they are all it can match on.
MIN_SEARCH_WORD = 3


def search_words(query: str) -> list[str]:
    """The query as words. Empty means "no query" — a search verb, unlike a listing,
    returns nothing for it rather than everything."""
    words = [w.lower() for w in (query or "").split() if w.strip()]
    return [w for w in words if len(w) >= MIN_SEARCH_WORD] or words


def score_sql(words: Sequence[str], weights: Mapping[str, int]) -> tuple[str, list[str]]:
    """A relevance expression: per query word, the heaviest column containing it, summed.

    Word-OR, not phrase: an agent and a user both type phrases, and a row matching three
    of four words is the hit they wanted (kn-c6e8fbf0). Weights are what keep a hit in a
    title above one buried in a description — the CASE tests columns heaviest-first, so
    each word scores once.
    """
    if not words:
        return "0", []
    cols = sorted(weights.items(), key=lambda kv: -kv[1])
    params: list[str] = []
    parts: list[str] = []
    for word in words:
        cases = " ".join(f"WHEN {col} LIKE ? THEN {wt}" for col, wt in cols)
        parts.append(f"(CASE {cases} ELSE 0 END)")
        params += [f"%{word}%"] * len(cols)
    return "(" + " + ".join(parts) + ")", params
