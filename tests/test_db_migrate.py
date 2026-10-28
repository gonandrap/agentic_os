"""db.add_missing_columns — two openers of one fresh store race the same ALTER"""

import sqlite3

import pytest

from jarvis import central_store, db


class _StalePragma:
    """A connection whose PRAGMA was read before a concurrent opener added the column"""

    def __init__(self, conn: sqlite3.Connection) -> None:
        self.conn = conn

    def execute(self, sql: str, *args):
        if sql.startswith("PRAGMA table_info"):
            return self.conn.execute("SELECT 'id' AS name")
        return self.conn.execute(sql, *args)


def _conn() -> sqlite3.Connection:
    conn = sqlite3.connect(":memory:")
    conn.row_factory = sqlite3.Row
    conn.execute("CREATE TABLE t (id INTEGER, extra TEXT)")
    return conn


def test_a_column_a_concurrent_opener_added_is_not_an_error():
    db.add_missing_columns(_StalePragma(_conn()), {"t": {"extra": "TEXT"}})


def test_any_other_alter_failure_still_raises():
    with pytest.raises(sqlite3.OperationalError):
        db.add_missing_columns(_StalePragma(_conn()), {"missing_table": {"x": "TEXT"}})


def test_a_missing_column_is_added():
    conn = _conn()
    db.add_missing_columns(conn, {"t": {"added": "TEXT NOT NULL DEFAULT ''"}})
    assert "added" in {r["name"] for r in conn.execute("PRAGMA table_info(t)")}


def test_a_fresh_central_store_needs_no_alter():
    conn = sqlite3.connect(":memory:")
    conn.executescript(central_store.SCHEMA)
    for table, columns in central_store.ADDED_COLUMNS.items():
        have = {r[1] for r in conn.execute(f"PRAGMA table_info({table})")}
        assert set(columns) <= have, table
