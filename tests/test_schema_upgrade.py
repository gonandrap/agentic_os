"""A database written by the release that is live must reach today's schema on open.

This covers both of the schemas a release can move: the per-project `jarvis.db`
(`ProjectStore`) and Neo's own `neo.db` (`NeoStore`). They have the same mechanism and
therefore the same trap.

`ProjectStore.__init__` runs `CREATE TABLE IF NOT EXISTS` — which is a **no-op** on a
table that already exists. So a brand new table arrives on an upgrade for free, but a new
*column* on an existing table does not: it has to be declared in `ADDED_COLUMNS`, which is
the only thing that ever emits `ALTER TABLE`. Forgetting that is silent at write time and
fatal at read time, on live data only — never in a test that builds its database fresh.

This is the test that builds it stale instead. `tests/data/schema-<tag>.sql` is the schema
as shipped in the release currently in production; opening a database built from it with
today's code must produce exactly what today's code produces from nothing.

**When this fails after you add a column:** add it to `ADDED_COLUMNS` too — the base
`CREATE TABLE` alone only reaches new installs. When a new release ships, refresh the
asset from the new tag (`git show <tag>:src/jarvis/project_store.py`).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from jarvis.neo_store import NeoStore
from jarvis.project_store import ProjectStore

SHIPPED_SCHEMA = Path(__file__).parent / "data" / "schema-jarvis-0.1.11.sql"
SHIPPED_NEO_SCHEMA = Path(__file__).parent / "data" / "neo-schema-jarvis-0.4.0.sql"


def schema_of(conn: sqlite3.Connection) -> dict[str, set[str]]:
    tables = [r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table' "
        "AND name NOT LIKE 'sqlite_%'")]
    return {t: {r[1] for r in conn.execute(f"PRAGMA table_info({t})")} for t in tables}


@pytest.fixture()
def current(tmp_path) -> dict[str, set[str]]:
    """What today's code builds from nothing."""
    store = ProjectStore(tmp_path / "fresh")
    try:
        return schema_of(store.conn)
    finally:
        store.close()


@pytest.fixture()
def current_neo(tmp_path) -> dict[str, set[str]]:
    """The same, for Neo's database."""
    store = NeoStore(tmp_path / "fresh-neo.db")
    try:
        return schema_of(store.conn)
    finally:
        store.close()


def test_a_database_from_the_live_release_upgrades_in_full(tmp_path, current):
    proj = tmp_path / "legacy"
    (proj / ".jarvis").mkdir(parents=True)
    old = sqlite3.connect(proj / ".jarvis" / "jarvis.db")
    old.executescript(SHIPPED_SCHEMA.read_text())
    old.commit()
    before = schema_of(old)
    old.close()

    store = ProjectStore(proj)          # the upgrade
    try:
        after = schema_of(store.conn)
    finally:
        store.close()

    assert before != current, (
        "the frozen asset already matches today's schema — it is no longer testing an "
        "upgrade; refresh it from the newest release tag")
    assert after == current, {
        t: sorted(cols - after.get(t, set()))
        for t, cols in current.items() if cols - after.get(t, set())
    }


def test_neos_database_from_the_live_release_upgrades_in_full(tmp_path, current_neo):
    """`neo.db` had no upgrade test at all until the panel work needed one.

    It is the harder of the two to notice: the OS opens exactly one `neo.db`, in the
    user's `JARVIS_HOME`, which every test replaces with an empty one — so a forgotten
    `ADDED_COLUMNS` entry here is invisible until production reads the column.
    """
    path = tmp_path / "legacy-neo.db"
    old = sqlite3.connect(path)
    old.executescript(SHIPPED_NEO_SCHEMA.read_text())
    old.commit()
    before = schema_of(old)
    old.close()

    store = NeoStore(path)              # the upgrade
    try:
        after = schema_of(store.conn)
        # the seat scope and the stranded-question fix, named so a failure says which
        assert {"attempts", "claimed_at"} <= after["questions"]
        assert "seat" in after["learnings"]
        assert "panel_opinions" in after, "a new TABLE arrives for free — this one didn't"
        # and the upgraded database actually works: the new columns are readable, and
        # the legacy rows they were bolted onto default to the global/unclaimed reading
        q = store.ask("proj_a", "wo-1", "which delimiter?")
        assert q["attempts"] == 0 and q["claimed_at"] is None
        assert store.add_learning("global", project="proj_a")["seat"] == ""
        assert [r["content"] for r in store.learnings("proj_a")] == ["global"]
    finally:
        store.close()

    assert before != current_neo, (
        "the frozen asset already matches today's schema — it is no longer testing an "
        "upgrade; refresh it from the newest release tag")
    assert after == current_neo, {
        t: sorted(cols - after.get(t, set()))
        for t, cols in current_neo.items() if cols - after.get(t, set())
    }


def test_the_columns_the_turn_runtime_reads_survive_the_upgrade(tmp_path):
    """The specific fields this transport depends on, named so a failure says which.

    `session_id` and `worktree` predate headless turns but became load-bearing with
    them: the first is now the conversation's permanent identity, the second is the
    directory a turn must resume from.
    """
    proj = tmp_path / "legacy"
    (proj / ".jarvis").mkdir(parents=True)
    old = sqlite3.connect(proj / ".jarvis" / "jarvis.db")
    old.executescript(SHIPPED_SCHEMA.read_text())
    old.commit()
    old.close()

    store = ProjectStore(proj)
    try:
        after = schema_of(store.conn)
        assert "wo_turns" in after, "the turn table never arrived"
        assert {"session_id", "worktree", "job_id"} <= after["work_orders"]
        # and the store can actually round-trip a turn on the upgraded database
        wo = store.create_work_order(title="after the upgrade", description="")
        store.update_work_order(wo["id"], session_id="s-1", worktree=wo["id"])
        turn = store.create_turn(wo["id"], kind="dispatch", prompt="go")
        store.finish_turn(turn["id"], "done", result="ok")
        assert store.latest_turn(wo["id"])["result"] == "ok"
        assert store.get_work_order(wo["id"])["session_id"] == "s-1"
    finally:
        store.close()
