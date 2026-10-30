"""SQLite helpers shared by the central and per-project stores"""

from __future__ import annotations

import json
import sqlite3
import time
import uuid
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

# WAL close vs open deadlocks across threads on these (kn-8e43df8e)
DEADLOCKING_SQLITE = ("3.51.0", "3.51.1")


def refuse_deadlocking_sqlite(version: str = sqlite3.sqlite_version) -> None:
    if version in DEADLOCKING_SQLITE:
        raise RuntimeError(
            f"SQLite {version} deadlocks WAL close vs open across threads (kn-8e43df8e); "
            "run `uv sync` to rebuild the venv on the pinned uv-managed CPython"
        )


refuse_deadlocking_sqlite()


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
    """One all-or-nothing unit of work, reentrant: the outermost one owns the commit"""
    if conn.in_transaction:
        # The connection is in autocommit (`connect`), so this is an explicit BEGIN of ours
        yield conn
        return
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except BaseException:
        conn.execute("ROLLBACK")
        raise


def add_missing_columns(conn: sqlite3.Connection,
                        added: Mapping[str, Mapping[str, str]]) -> None:
    """ALTER in each column a table lacks; a column a concurrent opener just added is not an error"""
    for table, columns in added.items():
        have = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
        for name, decl in columns.items():
            if name in have:
                continue
            try:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")
            except sqlite3.OperationalError as e:
                # Two processes opening one fresh store both saw the column missing
                if "duplicate column name" not in str(e):
                    raise


def _tables(conn: sqlite3.Connection) -> list[str]:
    return [r["name"] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]


def tables_with_column(conn: sqlite3.Connection, column: str) -> list[str]:
    """Every table in the live schema carrying `column`, sorted"""
    return sorted(t for t in _tables(conn) if any(
        c["name"] == column for c in conn.execute(f"PRAGMA table_info({t})")))


def delete_where(conn: sqlite3.Connection, tables: Sequence[str], column: str,
                 value: Any) -> dict[str, int]:
    """Delete rows whose `column` is `value` from each table, foreign-key children first, counted"""
    every = _tables(conn)
    counts: dict[str, int] = {}
    for parent in tables:
        for child in every:
            for fk in conn.execute(f"PRAGMA foreign_key_list({child})").fetchall():
                if fk["table"] != parent:
                    continue
                # An FK naming no column targets the parent's primary key
                target = fk["to"] or next(c["name"] for c in conn.execute(
                    f"PRAGMA table_info({parent})") if c["pk"])
                # Explicit even under ON DELETE CASCADE: sqlite reports no rowcount for a cascade
                n = conn.execute(
                    f"DELETE FROM {child} WHERE {fk['from']} IN "
                    f"(SELECT {target} FROM {parent} WHERE {column}=?)",
                    (value,)).rowcount
                counts[child] = counts.get(child, 0) + n
        counts[parent] = counts.get(parent, 0) + conn.execute(
            f"DELETE FROM {parent} WHERE {column}=?", (value,)).rowcount
    return counts


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


# Substring matching makes a one- or two-letter word match almost every row ("in" is inside "login")
MIN_SEARCH_WORD = 3


#: At most this many distinct words reach SQL
MAX_SEARCH_WORDS = 12


def cap_words(words: Sequence[str]) -> list[str]:
    """Distinct words, first occurrence kept, at most `MAX_SEARCH_WORDS`"""
    return list(dict.fromkeys(words))[:MAX_SEARCH_WORDS]


def search_words(query: str) -> list[str]:
    """The query as words"""
    words = [w.lower() for w in (query or "").split() if w.strip()]
    return cap_words([w for w in words if len(w) >= MIN_SEARCH_WORD] or words)


def score_sql(words: Sequence[str], weights: Mapping[str, int]) -> tuple[str, list[str]]:
    """A relevance expression: per query word, the heaviest column containing it, summed"""
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
