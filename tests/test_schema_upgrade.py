"""A database written by the release that is live must reach today's schema on open"""

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
    """What today's code builds from nothing"""
    store = ProjectStore(tmp_path / "fresh")
    try:
        return schema_of(store.conn)
    finally:
        store.close()


@pytest.fixture()
def current_neo(tmp_path) -> dict[str, set[str]]:
    """The same, for Neo's database"""
    store = NeoStore(tmp_path / "fresh-neo.db")
    try:
        return schema_of(store.conn)
    finally:
        store.close()


@pytest.fixture()
def current_central(tmp_path) -> dict[str, set[str]]:
    """The same, for the central database"""
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


def test_neos_database_from_the_live_release_upgrades_in_full(tmp_path, current_neo,
                                                              neo_projects):
    """`neo.db` had no upgrade test at all until the panel work needed one"""
    path = tmp_path / "legacy-neo.db"
    old = sqlite3.connect(path)
    old.executescript(SHIPPED_NEO_SCHEMA.read_text())
    old.execute("INSERT INTO learnings (ts, project, content, source) VALUES (1.0,?,?,?)",
                ("proj_a", "a ruling from before provenance", "review"))
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
        # and the upgraded database actually works: the new columns are readable
        q = store.ask("proj_a", "wo-1", "which delimiter?")
        assert q["attempts"] == 0 and q["claimed_at"] is None
        assert {"wo_id", "origin_kind", "origin_id"} <= after["learnings"]
        assert store.add_learning("global", project="proj_a")["seat"] == ""
        assert [r["content"] for r in store.learnings("proj_a")] == [
            "a ruling from before provenance", "global"]
        # A row that predates provenance stays blank: '' is the honest record, never a guess
        legacy = store.learnings("proj_a")[0]
        assert (legacy["wo_id"], legacy["origin_kind"], legacy["origin_id"]) == ("", "", "")
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
    """`os.db` had NO upgrade mechanism at all until retraction needed one"""
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
        # and the upgraded database actually works end to end: a legacy-shaped table accepts a write
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


def test_the_input_size_columns_arrive_on_an_existing_agent_calls_table(tmp_path):
    """`agent_calls` predates them, so `CREATE TABLE IF NOT EXISTS` is a no-op on a live `os.db` and only... (docs/superpowers/specs/2026-09-26-bounded-model-inputs.md)"""
    path = tmp_path / "legacy-os.db"
    old = sqlite3.connect(path)
    old.executescript(SHIPPED_CENTRAL_SCHEMA.read_text())
    old.execute("""CREATE TABLE IF NOT EXISTS agent_calls (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       ts REAL NOT NULL, project TEXT NOT NULL DEFAULT '',
                       wo_id TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL,
                       label TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',
                       question_id INTEGER, ok INTEGER NOT NULL DEFAULT 1,
                       cost_usd REAL, input INTEGER NOT NULL DEFAULT 0,
                       cache_write INTEGER NOT NULL DEFAULT 0,
                       cache_read INTEGER NOT NULL DEFAULT 0,
                       output INTEGER NOT NULL DEFAULT 0, usage_json TEXT)""")
    old.execute("INSERT INTO agent_calls (ts, kind, wo_id) VALUES (1.0, 'neo_answer',"
                " 'wo-old')")
    old.commit()
    before = schema_of(old)
    old.close()
    assert "prompt_chars" not in before["agent_calls"]

    store = CentralStore(path)         # the upgrade
    try:
        assert {"prompt_chars", "system_prompt_chars"} <= schema_of(
            store.conn)["agent_calls"]
        (legacy,) = store.agent_calls(wo_id="wo-old")
        assert (legacy["prompt_chars"], legacy["system_prompt_chars"]) == (0, 0)
        # and the upgraded table takes a measured write
        store.add_agent_call("neo_answer", wo_id="wo-new", prompt_chars=12_000,
                             system_prompt_chars=800)
        (fresh,) = store.agent_calls(wo_id="wo-new")
        assert (fresh["prompt_chars"], fresh["system_prompt_chars"]) == (12_000, 800)
    finally:
        store.close()


def test_the_latency_column_arrives_on_an_existing_agent_calls_table(tmp_path):
    """NULLABLE, unlike `prompt_chars` beside it: a sub-millisecond call rounds to 0 (docs/superpowers/specs/2026-10-01-neo-observability.md)"""
    path = tmp_path / "legacy-latency.db"
    old = sqlite3.connect(path)
    old.executescript(SHIPPED_CENTRAL_SCHEMA.read_text())
    old.execute("""CREATE TABLE IF NOT EXISTS agent_calls (
                       id INTEGER PRIMARY KEY AUTOINCREMENT,
                       ts REAL NOT NULL, project TEXT NOT NULL DEFAULT '',
                       wo_id TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL,
                       label TEXT NOT NULL DEFAULT '', model TEXT NOT NULL DEFAULT '',
                       question_id INTEGER, ok INTEGER NOT NULL DEFAULT 1,
                       cost_usd REAL, input INTEGER NOT NULL DEFAULT 0,
                       cache_write INTEGER NOT NULL DEFAULT 0,
                       cache_read INTEGER NOT NULL DEFAULT 0,
                       output INTEGER NOT NULL DEFAULT 0, usage_json TEXT)""")
    old.execute("INSERT INTO agent_calls (ts, kind, wo_id) VALUES (1.0, 'neo_answer',"
                " 'wo-old')")
    old.commit()
    before = schema_of(old)
    old.close()
    assert "latency_ms" not in before["agent_calls"]

    store = CentralStore(path)         # the upgrade
    try:
        assert "latency_ms" in schema_of(store.conn)["agent_calls"]
        (legacy,) = store.agent_calls(wo_id="wo-old")
        assert legacy["latency_ms"] is None, "an untimed call is absent, never 0 ms"
        store.add_agent_call("neo_answer", wo_id="wo-new", latency_ms=1234)
        (fresh,) = store.agent_calls(wo_id="wo-new")
        assert fresh["latency_ms"] == 1234
    finally:
        store.close()


def test_the_escalation_cause_column_arrives_on_an_existing_questions_table(tmp_path,
                                                                           neo_projects):
    """NULL on every pre-existing row (docs/superpowers/specs/2026-10-01-neo-observability.md)"""
    path = tmp_path / "legacy-cause.db"
    old = sqlite3.connect(path)
    old.executescript(SHIPPED_NEO_SCHEMA.read_text())
    old.execute("INSERT INTO questions (ts, project, wo_id, question, status, "
                "answer_reason) VALUES (1.0, 'proj_a', 'wo-old', 'which?', 'escalated',"
                " 'the user must decide: this touches production')")
    old.commit()
    assert "escalation_cause" not in schema_of(old)["questions"]
    old.close()

    store = NeoStore(path)              # the upgrade
    try:
        assert "escalation_cause" in schema_of(store.conn)["questions"]
        (legacy,) = [q for q in store.list_questions() if q["wo_id"] == "wo-old"]
        assert legacy["escalation_cause"] is None
        q = store.ask("proj_a", "wo-1", "and this?")
        store.mark(q["id"], "escalated", reason="yours", cause="high-stakes")
        assert store.get(q["id"])["escalation_cause"] == "high-stakes"
    finally:
        store.close()


def test_the_config_version_ledger_and_its_index_arrive_on_a_central_upgrade(tmp_path):
    """`schema_of` returns `{table: {columns}}`"""
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
    """`wo_turns` already ships in the live release"""
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


def test_reopens_at_reaches_a_database_that_already_has_health_reviews(tmp_path):
    """`health_reviews` already ships, so the hold's moment cannot arrive with the `CREATE TABLE`"""
    proj = tmp_path / "legacy-health"
    (proj / ".jarvis").mkdir(parents=True)
    store = ProjectStore(proj)
    store.record_health_review("work_order", "wo-1", fingerprint="fp",
                               trigger="first-look", outcome="failed", detail="old")
    store.close()
    conn = sqlite3.connect(proj / ".jarvis" / "jarvis.db")
    conn.execute("ALTER TABLE health_reviews DROP COLUMN reopens_at")
    conn.commit()
    conn.close()

    store = ProjectStore(proj)                      # the upgrade
    try:
        assert "reopens_at" in schema_of(store.conn)["health_reviews"]
        (old,) = store.health_reviews_of("work_order", "wo-1")
        assert old["reopens_at"] == 0.0
        assert store.health_sweep_hold() is None
    finally:
        store.close()


def test_the_columns_the_turn_runtime_reads_survive_the_upgrade(tmp_path):
    """The specific fields this transport depends on, named so a failure says which"""
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
    """`feature_orders` ships in the live release, so `base_sha` cannot arrive with the `CREATE TABLE`"""
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


def test_kind_reaches_a_database_that_already_has_feature_orders(tmp_path):
    """Same construction as `base_sha` above, and the same reason"""
    proj = tmp_path / "legacy"
    (proj / ".jarvis").mkdir(parents=True)
    store = ProjectStore(proj)                      # today's schema...
    aged = store.create_feature_order("CSV export", description="the whole ask")
    store.close()
    conn = sqlite3.connect(proj / ".jarvis" / "jarvis.db")
    conn.execute("ALTER TABLE feature_orders DROP COLUMN kind")       # ...aged
    conn.commit()
    conn.close()

    store = ProjectStore(proj)                      # the upgrade
    try:
        assert "kind" in schema_of(store.conn)["feature_orders"]
        assert store.get_feature_order(aged["id"])["kind"] == "feature"
        assert [r["id"] for r in store.list_feature_orders()] == [aged["id"]]
        # and both values round-trip on the upgraded database
        io = store.create_feature_order("slow turns", description="what I saw",
                                        kind="improvement")
        assert store.get_feature_order(io["id"])["kind"] == "improvement"
        assert [r["id"] for r in store.list_feature_orders(kind="improvement")] \
            == [io["id"]]
    finally:
        store.close()


def test_the_validation_tables_and_their_partial_indexes_arrive_on_an_upgrade(tmp_path):
    """A new TABLE arrives for free on `CREATE TABLE IF NOT EXISTS`"""
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


def test_config_version_reaches_a_database_that_already_has_rounds_and_work_orders(
        tmp_path):
    """`validation_rounds` already ships, so the round's stamp cannot arrive with the `CREATE TABLE`"""
    proj = tmp_path / "legacy"
    (proj / ".jarvis").mkdir(parents=True)
    ProjectStore(proj).close()                      # today's schema...
    conn = sqlite3.connect(proj / ".jarvis" / "jarvis.db")
    conn.execute("ALTER TABLE validation_rounds DROP COLUMN config_version")  # ...aged
    conn.execute("ALTER TABLE work_orders DROP COLUMN config_version")
    conn.commit()
    conn.close()

    store = ProjectStore(proj)                      # the upgrade
    try:
        after = schema_of(store.conn)
        assert "config_version" in after["validation_rounds"]
        assert "config_version" in after["work_orders"]
        # and both round-trip on the upgraded database
        wo = store.create_work_order(title="after the upgrade", description="")
        assert store.get_work_order(wo["id"])["config_version"] is None
        store.update_work_order(wo["id"], model="sonnet",         # the dispatch splat
                                config_version="cfg-a1b2c3d4e5f6")
        assert store.get_work_order(wo["id"])["config_version"] == "cfg-a1b2c3d4e5f6"
        rnd = store.open_validation_round(wo_id=wo["id"], fingerprint="a",
                                          config_version="cfg-a1b2c3d4e5f6")
        assert rnd["config_version"] == "cfg-a1b2c3d4e5f6"
    finally:
        store.close()


def test_legacy_cost_alarms_are_backfilled_exactly_once_however_often_the_store_opens(
        tmp_path):
    """`_migrate` runs in `ProjectStore.__init__`"""
    proj = tmp_path / "legacy"
    (proj / ".jarvis").mkdir(parents=True)
    old = sqlite3.connect(proj / ".jarvis" / "jarvis.db")
    old.executescript(SHIPPED_SCHEMA.read_text())
    old.execute("INSERT INTO work_orders (id, title, description, status, origin,"
                " created_at, updated_at) VALUES ('wo-legacy','the long one','',"
                "'running','jarvis',1.0,1.0)")
    for ts, payload in ((2.0, '{"kind":"long-turn","seq":3,"reason":"being billed"}'),
                        (3.0, '{"kind":"big-rewrite","seq":3,"reason":"re-sent it"}')):
        old.execute("INSERT INTO wo_events (wo_id, ts, kind, payload)"
                    " VALUES ('wo-legacy',?,'cost_alarm',?)", (ts, payload))
    old.commit()
    old.close()

    counts, rows = [], []
    for _ in range(3):
        store = ProjectStore(proj)          # the upgrade, three times over
        try:
            rows = store.alarms_of("wo-legacy")
            counts.append(len(rows))
        finally:
            store.close()

    assert counts == [2, 2, 2], "the backfill ran again on a later open"
    assert [r["kind"] for r in rows] == ["long-turn", "big-rewrite"]
    assert [r["seq"] for r in rows] == [3, 3]
    assert rows[0]["reason"] == "being billed"
    assert rows[0]["ts"] == 2.0, "dated when it was raised, not when it was backfilled"
    assert rows[0]["id"].startswith("al-")
    # SKIPPED, not RAISED: `raised` is the supervisor's work queue
    assert {r["status"] for r in rows} == {"skipped"}


def test_assumptions_that_predate_the_new_columns_still_read_as_the_users(tmp_path):
    """The schema tests above compare SHAPES"""
    from jarvis import ops
    from jarvis.project_store import ProjectStore as PS

    proj = tmp_path / "legacy-rows"
    (proj / ".jarvis").mkdir(parents=True)
    old = sqlite3.connect(proj / ".jarvis" / "jarvis.db")
    old.executescript(SHIPPED_SCHEMA.read_text())
    old.execute("INSERT INTO work_orders (id, title, description, status, "
                "created_at, updated_at) VALUES "
                "('wo-old', 't', 'd', 'needs_review', 1.0, 1.0)")
    old.execute("INSERT INTO assumptions (wo_id, ts, content, status) VALUES "
                "('wo-old', 1.0, 'picked postgres over sqlite', 'accepted')")
    old.commit()
    old.close()

    store = PS(proj)
    try:
        (row,) = store.all_assumptions("wo-old")
    finally:
        store.close()

    assert row["content"] == "picked postgres over sqlite"
    assert row["status"] == "accepted"
    # The five new columns arrived with their defaults, and the blank reads as the user
    assert row["decided_by"] == ""
    assert row["decided_reason"] == "" and row["decided_model"] == ""
    assert row["decided_config_version"] is None and row["neo_question_id"] is None
    assert ops.assumption_decider(row) == "you"
    assert "by you" in ops.assumption_line(row)


def test_a_turn_that_predates_the_context_ledger_reads_as_not_recorded(tmp_path,
                                                                      monkeypatch):
    """kn-c712a5d6: a column is untested until a test WRITES it and a test reads a row that PREDATES it"""
    from jarvis import ops
    from jarvis.project_store import ProjectStore as PS

    proj = tmp_path / "legacy-turns"
    (proj / ".jarvis").mkdir(parents=True)
    old = sqlite3.connect(proj / ".jarvis" / "jarvis.db")
    old.executescript(SHIPPED_SCHEMA.read_text())
    old.execute("INSERT INTO work_orders (id, title, description, status, created_at, "
                "updated_at) VALUES ('wo-old', 'ran before the ledger', 'd', "
                "'completed', 1.0, 1.0)")
    old.execute("CREATE TABLE wo_turns (id INTEGER PRIMARY KEY AUTOINCREMENT, "
                "wo_id TEXT NOT NULL REFERENCES work_orders(id), seq INTEGER NOT NULL, "
                "kind TEXT NOT NULL, msg_id INTEGER, prompt TEXT NOT NULL, "
                "state TEXT NOT NULL DEFAULT 'running', pid INTEGER, "
                "started_at REAL NOT NULL, ended_at REAL, exit_code INTEGER, "
                "result TEXT, error TEXT, cost_usd REAL, num_turns INTEGER, "
                "outfile TEXT NOT NULL DEFAULT '', errfile TEXT NOT NULL DEFAULT '')")
    old.execute("INSERT INTO wo_turns (wo_id, seq, kind, prompt, state, started_at, "
                "ended_at) VALUES ('wo-old', 1, 'dispatch', 'go', 'done', 1.0, 2.0)")
    old.commit()
    old.close()

    store = PS(proj)                                  # the upgrade
    try:
        assert "context_json" in schema_of(store.conn)["wo_turns"]
        (row,) = store.list_turns("wo-old")
        assert row["context_json"] is None             # not recorded, and not "{}"
    finally:
        store.close()

    # and the read path says so in words rather than rendering an empty table
    monkeypatch.setattr(ops, "registered_project_paths", lambda **_: {"legacy": proj})
    out = ops.context_report("wo-old")

    assert out["recorded"] is False
    assert "not recorded for this order" in out["note"]
    assert [t["seq"] for t in out["turns"]] == [1]
    assert out["turns"][0]["recorded"] is False and out["turns"][0]["ingredients"] == []
    assert "not recorded" in out["turns"][0]["note"]


def test_an_order_that_predates_the_self_evolution_columns_is_not_a_fix_order(tmp_path):
    """§7.1 of docs/superpowers/specs/2026-09-27-self-evolution.md"""
    from jarvis import rules

    proj = tmp_path / "legacy-fix-order"
    (proj / ".jarvis").mkdir(parents=True)
    old = sqlite3.connect(proj / ".jarvis" / "jarvis.db")
    old.executescript(SHIPPED_SCHEMA.read_text())
    old.execute("INSERT INTO work_orders (id, title, description, status, created_at, "
                "updated_at) VALUES ('wo-old', 'filed before gap classes existed', "
                "'mentions stale-hold in prose and nothing may read it', "
                "'completed', 1.0, 1.0)")
    old.commit()
    old.close()

    store = ProjectStore(proj)                                 # the upgrade
    try:
        assert {"gap_class", "detector_id", "remedy_rule_id",
                "trigger_wo_id"} <= schema_of(store.conn)["work_orders"]
        row = store.get_work_order("wo-old")
        assert row["gap_class"] is None
        assert row["detector_id"] is None
        assert row["remedy_rule_id"] is None
        assert row["trigger_wo_id"] is None
        assert rules.precheck([], {}, row, None) is None
    finally:
        store.close()


def test_an_order_that_predates_the_autopsy_column_reads_as_never_sealed(tmp_path):
    """kn-c712a5d6's other half for `autopsy_json`: `tests/test_autopsy.py` writes the column"""
    from jarvis import autopsy

    proj = tmp_path / "legacy-autopsy"
    (proj / ".jarvis").mkdir(parents=True)
    old = sqlite3.connect(proj / ".jarvis" / "jarvis.db")
    old.executescript(SHIPPED_SCHEMA.read_text())
    old.execute("INSERT INTO work_orders (id, title, description, status, created_at, "
                "updated_at) VALUES ('wo-old', 'ran before the autopsy', 'd', "
                "'completed', 1.0, 1.0)")
    old.commit()
    old.close()

    store = ProjectStore(proj)                                 # the upgrade
    try:
        assert {"autopsy_json", "autopsy_sealed_at"} <= schema_of(store.conn)[
            "work_orders"]
        row = store.get_work_order("wo-old")
        assert row["autopsy_json"] is None                     # never sealed, not "{}"
        assert row["autopsy_sealed_at"] is None
        assert autopsy.unseal(row) is None
        assert [o["id"] for o in store.unsealed_autopsy_orders()] == ["wo-old"]
    finally:
        store.close()


#: An arbitrary epoch the backfill fixtures hang off
SPAN_T0 = 1_700_000_000.0


def legacy_with_history(tmp_path) -> Path:
    """A database from the live release holding one work order's status events and one feature order"""
    import json

    proj = tmp_path / "legacy-spans"
    (proj / ".jarvis").mkdir(parents=True)
    old = sqlite3.connect(proj / ".jarvis" / "jarvis.db")
    old.executescript(SHIPPED_SCHEMA.read_text())
    old.execute(
        "INSERT INTO work_orders (id, title, description, status, origin, created_at, "
        "updated_at) VALUES ('wo-old', 't', '', 'completed', 'jarvis', ?, ?)",
        (SPAN_T0, SPAN_T0 + 400))
    for ts, status in ((100, "running"), (200, "needs_review"),
                       (250, "needs_review"), (300, "completed")):
        old.execute(
            "INSERT INTO wo_events (wo_id, ts, kind, payload) VALUES ('wo-old',?,?,?)",
            (SPAN_T0 + ts, "status", json.dumps({"status": status})))
    old.commit()
    old.close()
    return proj


def spans_of(store: ProjectStore, order_id: str) -> list[tuple]:
    return [(r["from_status"], r["to_status"], r["ts"], r["approximate"])
            for r in store.state_spans(order_id)]


def test_status_events_are_backfilled_as_spans_exactly(tmp_path):
    """The historical status trail, reproduced (docs/superpowers/specs/2026-09-27-time-in-each-state.md)"""
    store = ProjectStore(legacy_with_history(tmp_path))       # the upgrade
    try:
        assert spans_of(store, "wo-old") == [
            ("", "pending", SPAN_T0, 0),
            ("pending", "running", SPAN_T0 + 100, 0),
            ("running", "needs_review", SPAN_T0 + 200, 0),
            ("needs_review", "completed", SPAN_T0 + 300, 0),
        ]
    finally:
        store.close()


def test_a_historical_feature_order_gets_one_approximate_span(tmp_path):
    """A feature order has no event trail, so its history is one coarse span and says so"""
    proj = legacy_with_history(tmp_path)
    first = ProjectStore(proj)
    try:
        first.conn.execute(
            "INSERT INTO feature_orders (id, title, description, status, origin, "
            "created_at, updated_at) VALUES ('fo-old','f','','executing','jarvis',?,?)",
            (SPAN_T0, SPAN_T0 + 500))
        first.conn.commit()
        assert spans_of(first, "fo-old") == []
    finally:
        first.close()

    second = ProjectStore(proj)
    try:
        assert spans_of(second, "fo-old") == [("", "executing", SPAN_T0, 1)]
    finally:
        second.close()


def test_the_backfill_is_idempotent_across_opens(tmp_path):
    """`_migrate` runs in `__init__` — every CLI invocation and every reconcile"""
    proj = legacy_with_history(tmp_path)
    first = ProjectStore(proj)
    try:
        before = spans_of(first, "wo-old")
    finally:
        first.close()
    second = ProjectStore(proj)
    try:
        assert spans_of(second, "wo-old") == before
    finally:
        second.close()
