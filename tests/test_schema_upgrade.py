"""A database written by the release that is live must reach today's schema on open.

This covers all three of the schemas a release can move: the per-project `jarvis.db`
(`ProjectStore`), Neo's own `neo.db` (`NeoStore`) and the central `os.db`
(`CentralStore`). They have the same mechanism and therefore the same trap.

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

from jarvis.central_store import CentralStore
from jarvis.neo_store import NeoStore
from jarvis.project_store import ProjectStore

SHIPPED_SCHEMA = Path(__file__).parent / "data" / "schema-jarvis-0.1.11.sql"
SHIPPED_NEO_SCHEMA = Path(__file__).parent / "data" / "neo-schema-jarvis-0.4.0.sql"
SHIPPED_CENTRAL_SCHEMA = Path(__file__).parent / "data" / "central-schema-jarvis-0.4.0.sql"


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


@pytest.fixture()
def current_central(tmp_path) -> dict[str, set[str]]:
    """The same, for the central database."""
    store = CentralStore(tmp_path / "fresh-os.db")
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


def test_the_central_database_from_the_live_release_upgrades_in_full(
        tmp_path, current_central):
    """`os.db` had NO upgrade mechanism at all until retraction needed one.

    `CentralStore.__init__` ran `executescript(SCHEMA)` and nothing else, so the first
    column ever added to a central table had to bring `ADDED_COLUMNS` + `_migrate()`
    with it — otherwise the column reaches new installs only, and every live `os.db`
    raises `no such column` the moment a read names it.
    """
    path = tmp_path / "legacy-os.db"
    old = sqlite3.connect(path)
    old.executescript(SHIPPED_CENTRAL_SCHEMA.read_text())
    old.commit()
    before = schema_of(old)
    old.close()

    store = CentralStore(path)         # the upgrade
    try:
        after = schema_of(store.conn)
        assert {"retired_at", "retired_reason"} <= after["knowledge"]
        # and the upgraded database actually works end to end: a legacy-shaped table
        # accepts a write, the new column defaults to "standing", and both the prompt
        # feed and the retraction round-trip on it
        row = store.add_knowledge("always run make lint", project="proj_a", topic="ci")
        live = store.add_knowledge("prefer uv over pip", project="")
        assert [r["content"] for r in store.relevant_knowledge("proj_a")] == [
            live["content"], row["content"]]
        store.retract_knowledge(row["id"], "make was replaced by just")
        offered = [r["content"] for r in store.relevant_knowledge("proj_a")]
        assert offered == [live["content"]]                     # gone from the feed
        assert len(store.search_knowledge("")) == 2             # still in the record
    finally:
        store.close()

    assert before != current_central, (
        "the frozen asset already matches today's schema — it is no longer testing an "
        "upgrade; refresh it from the newest release tag")
    assert after == current_central, {
        t: sorted(cols - after.get(t, set()))
        for t, cols in current_central.items() if cols - after.get(t, set())
    }


def test_the_config_version_ledger_and_its_index_arrive_on_a_central_upgrade(tmp_path):
    """`schema_of` returns `{table: {columns}}`, so an index is invisible to every
    column comparison in this file — including the one directly above, which would pass
    with `idx_os_config_versions_ts` missing.

    The table itself arrives free on `CREATE TABLE IF NOT EXISTS`; the index does so only
    because it is in `SCHEMA` beside it. Same trap as the validation tables' partial
    unique indexes, one database over.
    """
    path = tmp_path / "legacy-os.db"
    old = sqlite3.connect(path)
    old.executescript(SHIPPED_CENTRAL_SCHEMA.read_text())
    old.commit()
    old.close()

    store = CentralStore(path)         # the upgrade
    try:
        assert "os_config_versions" in schema_of(store.conn)
        indexes = {r[0] for r in store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        assert "idx_os_config_versions_ts" in indexes
        # and the ledger works on the UPGRADED database, not merely exists on it
        row = store.add_config_version({"os": {"ui": {"port": 9000}}},
                                       {"os.ui.port": 9000}, actor="user",
                                       reason="moved the dashboard")
        assert store.head_config_version()["id"] == row["id"]
        assert store.add_config_version({"os": {"ui": {"port": 9000}}}, {},
                                        actor="file")["id"] == row["id"]
        assert len(store.config_versions()) == 1
    finally:
        store.close()


def test_the_usage_json_column_reaches_a_database_that_already_has_wo_turns(tmp_path):
    """`wo_turns` already ships in the live release, so `usage_json` cannot arrive
    with the `CREATE TABLE` — it must be in `ADDED_COLUMNS`.

    The stale database is built by dropping the column from today's schema rather
    than from a frozen asset: the project asset (0.1.11) predates `wo_turns`
    entirely, so the upgrade it exercises creates the whole table fresh and would
    pass with `ADDED_COLUMNS` forgotten — exactly the silent failure this file
    exists to catch.
    """
    proj = tmp_path / "legacy"
    (proj / ".jarvis").mkdir(parents=True)
    ProjectStore(proj).close()                      # today's schema...
    conn = sqlite3.connect(proj / ".jarvis" / "jarvis.db")
    conn.execute("ALTER TABLE wo_turns DROP COLUMN usage_json")   # ...aged one release
    conn.commit()
    conn.close()

    store = ProjectStore(proj)                      # the upgrade
    try:
        assert "usage_json" in schema_of(store.conn)["wo_turns"]
        # and it round-trips on the upgraded database
        wo = store.create_work_order(title="after the upgrade", description="")
        turn = store.create_turn(wo["id"], kind="dispatch", prompt="go")
        store.finish_turn(turn["id"], "done", result="ok",
                          usage_json='{"input": 2, "output": 941}')
        assert store.latest_turn(wo["id"])["usage_json"] == '{"input": 2, "output": 941}'
    finally:
        store.close()


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


def test_base_sha_reaches_a_database_that_already_has_feature_orders(tmp_path):
    """`feature_orders` ships in the live release, so `base_sha` cannot arrive with the
    `CREATE TABLE` — it must be in `ADDED_COLUMNS`.

    Same construction, and the same reason, as the `usage_json` test above: the frozen
    project asset (0.1.11) predates feature orders entirely, so the upgrade it exercises
    creates the whole table fresh and would pass with `ADDED_COLUMNS` forgotten. The
    stale database is therefore built by aging today's schema by one column instead.
    """
    proj = tmp_path / "legacy"
    (proj / ".jarvis").mkdir(parents=True)
    ProjectStore(proj).close()                      # today's schema...
    conn = sqlite3.connect(proj / ".jarvis" / "jarvis.db")
    conn.execute("ALTER TABLE feature_orders DROP COLUMN base_sha")   # ...aged
    conn.commit()
    conn.close()

    store = ProjectStore(proj)                      # the upgrade
    try:
        assert "base_sha" in schema_of(store.conn)["feature_orders"]
        # and it round-trips on the upgraded database
        fo = store.create_feature_order("CSV export", description="the whole ask")
        assert store.get_feature_order(fo["id"])["base_sha"] is None
        store.update_feature_order(fo["id"], base_sha="deadbeef")
        assert store.get_feature_order(fo["id"])["base_sha"] == "deadbeef"
    finally:
        store.close()


def test_the_validation_tables_and_their_partial_indexes_arrive_on_an_upgrade(tmp_path):
    """A new TABLE arrives for free on `CREATE TABLE IF NOT EXISTS` — its INDEXES only
    do if they are in `SCHEMA` too, and the whole polymorphic design rests on two
    partial unique indexes that no column comparison would ever notice were missing."""
    proj = tmp_path / "legacy"
    (proj / ".jarvis").mkdir(parents=True)
    old = sqlite3.connect(proj / ".jarvis" / "jarvis.db")
    old.executescript(SHIPPED_SCHEMA.read_text())
    old.commit()
    old.close()

    store = ProjectStore(proj)                      # the upgrade
    try:
        after = schema_of(store.conn)
        assert {"validation_rounds", "validation_opinions"} <= set(after)
        indexes = {r[0] for r in store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='index'")}
        assert {"validation_rounds_wo", "validation_rounds_fo"} <= indexes
        # and they bite on the upgraded database, not just exist on it
        wo = store.create_work_order("after the upgrade")
        store.open_validation_round(wo_id=wo["id"], fingerprint="a")
        with pytest.raises(sqlite3.IntegrityError):
            store.conn.execute(
                "INSERT INTO validation_rounds (wo_id, round, ts, fingerprint)"
                " VALUES (?,1,1.0,'dup')", (wo["id"],))
    finally:
        store.close()
