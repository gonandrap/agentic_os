"""Central store: $JARVIS_HOME/os.db

Holds everything that must be unified across projects: the project registry, the
notification inbox, the backlog (with dependencies), and the knowledge base.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Sequence

from . import db
from .paths import central_db_path, ensure_home

# Tag marking knowledge mirrored out of a Claude Code memory file rather than typed
# by a worker via `jarvis learn add`.
MEMORY_TAG = "claude-memory"

# Tag marking knowledge that is injected into every worker prompt in full, instead of
# only as a headline in the index. Reserved for safety rails a worker must not be able
# to miss by failing to search.
PINNED_TAG = "pinned"

# How much of an entry survives into the index line. Long enough for a full short
# learning (most are one sentence), short enough that 40 of them cost ~1.5k tokens.
HEADLINE_CHARS = 160

# Read verbs that AIM at an entry, as against the two that sweep the index. Only these
# record per-entry hits: `list` returns everything, so counting it would mark the whole
# base as consulted and destroy the one number that says which entries earn their place.
AIMED_VERBS = ("show", "search")


def split_tags(tags: str) -> list[str]:
    return [t for t in (s.strip() for s in (tags or "").split(",")) if t]


def has_tag(tags: str, tag: str) -> bool:
    return tag in split_tags(tags)


def fts_query(term: str) -> str:
    """A user's words as an FTS5 OR-query. '' when nothing in them is searchable.

    Each word becomes a quoted phrase, so `-`, `:` and `"` reach the tokenizer as text
    instead of as FTS5 syntax — see
    docs/superpowers/specs/2026-08-24-ranked-knowledge-search.md §5.
    """
    words = [w for w in (term or "").split() if any(c.isalnum() for c in w)]
    return " OR ".join('"' + w.replace('"', '""') + '"' for w in words)


def headline(content: str, limit: int = HEADLINE_CHARS) -> str:
    """One-line gist of an entry: its first line, truncated.

    Mirrored memory files are whole documents; taking the first line keeps a 4 KB entry
    from costing 4 KB in an index whose entire point is to be cheap.
    """
    text = " ".join((content or "").strip().split("\n", 1)[0].split())
    if len(text) <= limit:
        return text
    return text[: limit - 1].rstrip() + "…"


@dataclass
class KnowledgeBrief:
    """What a worker prompt says about the knowledge base.

    Deliberately *not* the knowledge itself: `pinned` carries full text for the few
    entries that were curated as unmissable, `digest` carries headlines + ids so the
    worker can fetch what it needs, and `overflow` names the topics that did not fit
    so nothing is silently invisible.
    """
    project: str
    total: int = 0
    pinned: list[dict[str, Any]] = field(default_factory=list)
    digest: list[dict[str, Any]] = field(default_factory=list)
    overflow: list[tuple[str, int]] = field(default_factory=list)

    @property
    def overflow_count(self) -> int:
        return sum(n for _, n in self.overflow)

    def __bool__(self) -> bool:
        return self.total > 0

SCHEMA = """
CREATE TABLE IF NOT EXISTS projects (
    name TEXT PRIMARY KEY,
    path TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    model TEXT,
    status TEXT NOT NULL DEFAULT 'active',  -- active | stopped
    last_seen REAL,
    catalog_json TEXT
);
CREATE TABLE IF NOT EXISTS inbox (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    project TEXT NOT NULL,
    level TEXT NOT NULL DEFAULT 'info',
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    wo_id TEXT,
    status TEXT NOT NULL DEFAULT 'new',     -- new | notified | acked
    sink_results TEXT
);
CREATE TABLE IF NOT EXISTS backlog (
    id TEXT PRIMARY KEY,
    project TEXT NOT NULL,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'open',    -- open | promoted | done | dropped
    depends_on TEXT NOT NULL DEFAULT '[]',  -- JSON list of backlog ids
    promoted_wo_id TEXT,
    created_at REAL NOT NULL,
    -- Where this item came from. NULL/'' on anything a human typed; filled in when a
    -- work order deferred it, whoever ends up filing the row (see ADDED_COLUMNS).
    origin_wo_id TEXT,                      -- the work order that suggested it
    origin_fo_id TEXT,                      -- the feature order whose plan it came from
    origin_note TEXT NOT NULL DEFAULT ''    -- the why, and the Neo question id if given
);
CREATE TABLE IF NOT EXISTS knowledge (
    id TEXT PRIMARY KEY,
    project TEXT NOT NULL DEFAULT '',       -- '' = global
    ts REAL NOT NULL,
    topic TEXT NOT NULL DEFAULT '',
    content TEXT NOT NULL,
    tags TEXT NOT NULL DEFAULT '',
    retired_at REAL,                        -- NULL = standing; set = superseded
    retired_reason TEXT                     -- why, in the user's words
);
CREATE TABLE IF NOT EXISTS os_state (
    key TEXT PRIMARY KEY,
    value TEXT
);
-- One retrieval FROM the knowledge base, recorded where it happens.
--
-- Until this table existed the OS could say what it KNEW and nothing at all about what
-- was READ: whether workers consult the base, which entries earn their place, which are
-- dead weight, and which questions it is asked and cannot answer. The only evidence was
-- an opt-in paid eval somebody had to remember to run. See
-- docs/superpowers/specs/2026-08-23-what-memory-costs-and-who-reads-it.md.
--
-- Recorded at the read for the same reason `agent_calls` is recorded at the call: a
-- `jarvis learn show` leaves no trace anywhere else, so nothing can recover it later.
--
-- `chars` is what the read COST — content characters handed back — which is the only
-- honest measure of the knowledge base's share of a worker's context. The index in the
-- prompt is a fixed budget; this is the variable part.
CREATE TABLE IF NOT EXISTS knowledge_reads (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    project TEXT NOT NULL DEFAULT '',
    wo_id TEXT NOT NULL DEFAULT '',         -- '' = no work order: a person at a terminal
    verb TEXT NOT NULL,                     -- show | search | list | topics
    term TEXT NOT NULL DEFAULT '',          -- the query, or the ids asked for
    hits INTEGER NOT NULL DEFAULT 0,        -- rows returned
    chars INTEGER NOT NULL DEFAULT 0        -- content characters returned
);
-- Which entries a read actually returned, so "how often was THIS consulted" and "what
-- has never been read" are one GROUP BY rather than a scan of `term` strings.
CREATE TABLE IF NOT EXISTS knowledge_read_hits (
    read_id INTEGER NOT NULL REFERENCES knowledge_reads(id) ON DELETE CASCADE,
    kn_id TEXT NOT NULL
);
-- One Claude call the OS made on its OWN behalf, and the work order it was made for.
--
-- A work order's spend is not just its worker's turns: every question it asked Neo, every
-- seat of the panel that deliberated on it, every digest written for the dashboard is a
-- `claude -p` call Jarvis paid for BECAUSE of that work order. Those calls have no session
-- Jarvis owns and no transcript it can attribute, so unlike worker turns they cannot be
-- recovered after the fact — recording them at the moment they happen is the only way they
-- are ever counted. `usage.py`'s opening line ("Jarvis records no token usage of its own")
-- stopped being true here.
--
-- Central rather than per-project or in `neo.db`: Neo, the panel and the digest are three
-- subsystems and future OS calls will be a fourth, and os.db is the store that already
-- unifies across projects and already carries the work order's purge path.
--
-- Token classes are columns AND `usage_json` on purpose: the columns are what the fleet
-- report sums in SQL over every work order at once, and the JSON keeps the full envelope
-- (the ephemeral 1h/5m split, the per-call context peak) for anyone reading one call.
CREATE TABLE IF NOT EXISTS agent_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    project TEXT NOT NULL DEFAULT '',
    wo_id TEXT NOT NULL DEFAULT '',         -- '' = OS work no work order caused
    kind TEXT NOT NULL,                     -- neo_answer | panel_seat | digest | ...
    label TEXT NOT NULL DEFAULT '',         -- the seat name, or whatever names the call
    model TEXT NOT NULL DEFAULT '',
    question_id INTEGER,                    -- the neo.db question, where there is one
    -- The session the CLI minted for this one-shot call. Stored so an OS call can be
    -- opened up per API CALL the same way a worker turn now can: its transcript is the
    -- only place that detail exists, and this id is the only handle on it. Recorded at
    -- the call because nothing can recover it afterwards — the same reason the token
    -- counts beside it are.
    session_id TEXT NOT NULL DEFAULT '',
    ok INTEGER NOT NULL DEFAULT 1,
    cost_usd REAL,                          -- the CLI's own figure — exact, not a proxy
    input INTEGER NOT NULL DEFAULT 0,
    cache_write INTEGER NOT NULL DEFAULT 0,
    cache_read INTEGER NOT NULL DEFAULT 0,
    output INTEGER NOT NULL DEFAULT 0,
    usage_json TEXT
);
-- What the OS believes is a privileged action, and what it has LEARNED is not.
--
-- The recognisers behind the gates used to be regex tuples in `gates.KINDS`, and the
-- problem with that was not the regexes — it was that the table had no writer. Every
-- false positive was reviewed by Neo, correctly identified, dismissed, and forgotten;
-- the next work order in the next project tripped the same gate on the same shape. The
-- only path from "the reviewer knows this is harmless" to "the OS stops asking" ran
-- through a human filing a work order to widen a pattern.
--
-- Central rather than per-project, and that is the whole point rather than a filing
-- decision: a dismissal in one project has to settle the question for the next one.
-- The `project`/`wo_id`/`approval_id` columns are provenance — where this was learned —
-- not scope.
--
-- Three roles, and the third is what makes the first two safe to change: `match`
-- recognises an attempt, `exempt` clears a mention, and `canary` is a command that must
-- always gate, which every proposed exemption is tested against before it may exist.
-- See gate_rules.py.
CREATE TABLE IF NOT EXISTS gate_rules (
    id TEXT PRIMARY KEY,
    ts REAL NOT NULL,
    role TEXT NOT NULL,                     -- match | exempt | canary
    kind TEXT NOT NULL DEFAULT '',          -- gate name; '' on an exemption = every gate
    test TEXT NOT NULL,                     -- regex | signature | command
    pattern TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL DEFAULT 'builtin', -- builtin | neo | user
    project TEXT NOT NULL DEFAULT '',       -- provenance, never scope
    wo_id TEXT NOT NULL DEFAULT '',
    approval_id INTEGER,                    -- the dismissal this was learned from
    reason TEXT NOT NULL DEFAULT '',        -- the reviewer's words, verbatim
    hits INTEGER NOT NULL DEFAULT 0,        -- how often it has cleared a command
    last_hit REAL,
    retired_at REAL,                        -- NULL = in force; set = retracted
    retired_reason TEXT NOT NULL DEFAULT ''
);
-- The append-only history of what the fleet was configured to run, and the only place
-- that record exists: `projects.catalog_json` holds the CURRENT project dict and is
-- overwritten on every `jarvis start`, and the catalog file is untracked, so git is not
-- the history either. See
-- docs/superpowers/specs/2026-08-27-the-config-console.md §2, §9.
--
-- Fleet-wide rather than per project, because project settings resolve AGAINST the `os`
-- block at parse time, several settings have no project to belong to, and traceability
-- wants one id to stamp on a work order. "Per project" is served as a view, not as
-- separate counters.
--
-- TWO JSON documents with two different jobs, and the second is what makes the ledger
-- survive a release. `document_json` is the catalog file's own raw JSON, canonicalised —
-- what the file is rewritten FROM and what the content-addressed id hashes.
-- `resolved_json` is that document parsed and flattened to `path -> value` with every
-- default MATERIALISED at write time, which is what "which config judged this work
-- order" reads: a snapshot storing only the user's sparse keys would silently change
-- meaning on the day a shipped default moved (§2).
--
-- Rows are never rewritten and never migrated. A historical version is evidence of what
-- ran, not configuration anyone needs to run, so it is rendered and diffed — never fed
-- back to `parse_catalog` (§6).
CREATE TABLE IF NOT EXISTS os_config_versions (
    id             TEXT PRIMARY KEY,        -- cfg-<sha256(document_json)[:16]>
    ts             REAL NOT NULL,
    actor          TEXT NOT NULL,           -- user | file | release | <wo-id>
    reason         TEXT NOT NULL DEFAULT '',
    schema_version TEXT NOT NULL,           -- bugreport.jarvis_version() at write time
    document_json  TEXT NOT NULL,           -- canonical catalog document; APPLIED  (§2)
    resolved_json  TEXT NOT NULL,           -- path -> value, defaults frozen; EVIDENCE (§2)
    changes_json   TEXT NOT NULL DEFAULT '[]',  -- the edits the actor asked for
    source_path    TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_os_config_versions_ts ON os_config_versions(ts);
CREATE INDEX IF NOT EXISTS idx_gate_rules_role ON gate_rules(role, kind);
CREATE INDEX IF NOT EXISTS idx_inbox_status ON inbox(status);
CREATE INDEX IF NOT EXISTS idx_backlog_project ON backlog(project, status);
CREATE INDEX IF NOT EXISTS idx_agent_calls_wo ON agent_calls(wo_id, ts);
CREATE INDEX IF NOT EXISTS idx_knowledge_reads_wo ON knowledge_reads(wo_id, ts);
CREATE INDEX IF NOT EXISTS idx_knowledge_read_hits ON knowledge_read_hits(kn_id);
"""

# The ranked half of `search_knowledge` — see
# docs/superpowers/specs/2026-08-24-ranked-knowledge-search.md §4. Separate from SCHEMA
# because FTS5 is a compile-time option and a store that will not open is worse than a
# search that is merely as good as yesterday's (§8).
FTS_SCHEMA = """
CREATE VIRTUAL TABLE IF NOT EXISTS knowledge_fts USING fts5(
    content, topic, tags,
    content='knowledge', content_rowid='rowid', tokenize="porter unicode61"
);
CREATE TRIGGER IF NOT EXISTS knowledge_fts_ai AFTER INSERT ON knowledge BEGIN
    INSERT INTO knowledge_fts (rowid, content, topic, tags)
    VALUES (new.rowid, new.content, new.topic, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS knowledge_fts_ad AFTER DELETE ON knowledge BEGIN
    INSERT INTO knowledge_fts (knowledge_fts, rowid, content, topic, tags)
    VALUES ('delete', old.rowid, old.content, old.topic, old.tags);
END;
CREATE TRIGGER IF NOT EXISTS knowledge_fts_au AFTER UPDATE ON knowledge BEGIN
    INSERT INTO knowledge_fts (knowledge_fts, rowid, content, topic, tags)
    VALUES ('delete', old.rowid, old.content, old.topic, old.tags);
    INSERT INTO knowledge_fts (rowid, content, topic, tags)
    VALUES (new.rowid, new.content, new.topic, new.tags);
END;
"""

# One-time backfill marker, NOT a row count — a count on an external-content table
# re-scans `knowledge` and would hide a broken trigger by rebuilding on every open (§4).
FTS_BUILT_KEY = "knowledge_fts_built"

# content, topic, tags. A query word in the TOPIC outranks one buried in a long body (§6).
FTS_WEIGHTS = (1.0, 4.0, 2.0)

# Columns added after the first release, exactly as in `neo_store` and `project_store`.
# `CREATE TABLE IF NOT EXISTS` is a NO-OP on a table that already exists, so a column
# added to SCHEMA alone reaches new installs only and every live `os.db` fails on read.
# Until this existed `os.db` had no upgrade path at all — `CentralStore.__init__` ran
# `executescript(SCHEMA)` and nothing else — which is why the first column ever added to
# it had to bring the mechanism with it.
ADDED_COLUMNS = {
    "knowledge": {
        # Retraction. NULL on every pre-existing row, which reads as "standing".
        "retired_at": "REAL",
        "retired_reason": "TEXT",
    },
    "backlog": {
        # Where a deferred item came from. The backlog predates deferral routing, so
        # every pre-existing row reads as "somebody typed this" — NULL origins and an
        # empty note — which is exactly what it was.
        "origin_wo_id": "TEXT",
        "origin_fo_id": "TEXT",
        "origin_note": "TEXT NOT NULL DEFAULT ''",
    },
    "agent_calls": {
        # Which session the call ran in, so its per-API-call detail can be read back
        # from the transcript. '' on every pre-existing row, which reads as "not
        # recorded" — those calls keep their totals and simply cannot be expanded.
        "session_id": "TEXT NOT NULL DEFAULT ''",
    },
}


class CentralStore:
    def __init__(self, path: Path | None = None):
        ensure_home()
        self.db_path = path or central_db_path()
        self.conn = db.connect(self.db_path)
        self.conn.executescript(SCHEMA)
        self._migrate()
        self.fts = self._ensure_fts()
        self._seed_gate_rules()

    def _migrate(self) -> None:
        for table, columns in ADDED_COLUMNS.items():
            have = {
                r["name"]
                for r in self.conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            for name, decl in columns.items():
                if name not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

    def _ensure_fts(self) -> bool:
        """Create the search index and backfill it once. False = this SQLite has no FTS5.

        docs/superpowers/specs/2026-08-24-ranked-knowledge-search.md §4, §8.
        """
        try:
            self.conn.executescript(FTS_SCHEMA)
            if not self.get_state(FTS_BUILT_KEY):
                self.conn.execute(
                    "INSERT INTO knowledge_fts (knowledge_fts) VALUES ('rebuild')")
                self.set_state(FTS_BUILT_KEY, str(db.now()))
        except sqlite3.Error:
            return False
        return True

    def close(self) -> None:
        self.conn.close()

    # -- projects registry ----------------------------------------------------

    def upsert_project(self, name: str, path: str, description: str = "",
                       model: str | None = None, catalog_json: str = "{}") -> None:
        self.conn.execute(
            """INSERT INTO projects (name, path, description, model, status, last_seen, catalog_json)
               VALUES (?,?,?,?,'active',?,?)
               ON CONFLICT(name) DO UPDATE SET path=excluded.path,
                   description=excluded.description, model=excluded.model,
                   status='active', catalog_json=excluded.catalog_json""",
            (name, str(path), description, model, db.now(), catalog_json),
        )

    def touch_project(self, name: str) -> None:
        self.conn.execute("UPDATE projects SET last_seen=? WHERE name=?", (db.now(), name))

    def set_project_status(self, name: str, status: str) -> None:
        self.conn.execute("UPDATE projects SET status=? WHERE name=?", (status, name))

    def list_projects(self) -> list[dict[str, Any]]:
        return db.rows_to_dicts(
            self.conn.execute("SELECT * FROM projects ORDER BY name").fetchall()
        )

    def get_project(self, name: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM projects WHERE name=?", (name,)).fetchone()
        return dict(row) if row else None

    def project_name_for_path(self, path: str | Path) -> str:
        """The catalog name of the project checked out at `path`.

        A `ProjectStore` knows a path and nothing else, and several callers hold only a
        store — the message bus resolving an envelope's project, the validation panel
        scoping a seat's knowledge base. The registry maps name to path, so the path
        resolves back: a LOOKUP rather than a guess. A project not in the registry falls
        back to its directory name, which is what `jarvis adopt` would have named it.

        ONE IMPLEMENTATION, because two would drift: a panel that scoped its knowledge to
        a name the bus does not use would read a different project's standing
        instructions, and nothing on either side would look wrong.
        """
        try:
            here = Path(path).resolve()
        except OSError:  # pragma: no cover - a path that cannot be resolved is still usable
            here = Path(path)
        for row in self.list_projects():
            try:
                if Path(row["path"]).resolve() == here:
                    return str(row["name"])
            except OSError:  # pragma: no cover
                continue
        return here.name

    # -- inbox ------------------------------------------------------------------

    def add_inbox(self, project: str, title: str, body: str = "", level: str = "info",
                  wo_id: str | None = None) -> int:
        cur = self.conn.execute(
            "INSERT INTO inbox (ts, project, level, title, body, wo_id) VALUES (?,?,?,?,?,?)",
            (db.now(), project, level, title, body, wo_id),
        )
        return int(cur.lastrowid)

    def purge_work_order(self, wo_id: str) -> dict[str, int]:
        """Drop every central trace of a deleted work order.

        Inbox items about a work order that no longer exists are noise, and a backlog
        item whose promoted order was deleted goes back to open rather than pointing
        at a ghost.

        The OS's own calls for it go too: `wo delete` is documented as erasing the work
        order and its whole history, and spend attributed to an id nothing can resolve
        would sit in the fleet total for ever with no page able to explain it.
        """
        inbox = self.conn.execute("DELETE FROM inbox WHERE wo_id=?", (wo_id,)).rowcount
        calls = self.conn.execute("DELETE FROM agent_calls WHERE wo_id=?",
                                  (wo_id,)).rowcount
        reads = self.conn.execute("DELETE FROM knowledge_reads WHERE wo_id=?",
                                  (wo_id,)).rowcount
        backlog = self.conn.execute(
            """UPDATE backlog SET status='open', promoted_wo_id=NULL
               WHERE promoted_wo_id=? AND status='promoted'""",
            (wo_id,),
        ).rowcount
        return {"inbox": inbox, "agent_calls": calls, "knowledge_reads": reads,
                "backlog_reopened": backlog}

    def unacked_inbox(self, level: str | None = None) -> list[dict[str, Any]]:
        if level:
            rows = self.conn.execute(
                "SELECT * FROM inbox WHERE status != 'acked' AND level=? ORDER BY ts DESC", (level,)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM inbox WHERE status != 'acked' ORDER BY ts DESC"
            ).fetchall()
        return db.rows_to_dicts(rows)

    def new_inbox(self) -> list[dict[str, Any]]:
        return db.rows_to_dicts(
            self.conn.execute("SELECT * FROM inbox WHERE status='new' ORDER BY ts").fetchall()
        )

    def mark_inbox(self, inbox_id: int, status: str, sink_results: Any = None) -> None:
        self.conn.execute(
            "UPDATE inbox SET status=?, sink_results=COALESCE(?, sink_results) WHERE id=?",
            (status, db.to_json(sink_results) if sink_results is not None else None, inbox_id),
        )

    def ack_inbox(self, inbox_id: int | None = None) -> int:
        """Ack one item, or all when inbox_id is None. Returns rows affected."""
        if inbox_id is None:
            cur = self.conn.execute("UPDATE inbox SET status='acked' WHERE status != 'acked'")
        else:
            cur = self.conn.execute("UPDATE inbox SET status='acked' WHERE id=?", (inbox_id,))
        return cur.rowcount

    # -- backlog ------------------------------------------------------------------

    def add_backlog(self, project: str, title: str, description: str = "",
                    depends_on: list[str] | None = None, item_id: str | None = None,
                    origin_wo_id: str | None = None, origin_fo_id: str | None = None,
                    origin_note: str = "") -> dict[str, Any]:
        """File a backlog item. The three `origin_*` arguments are the relationship.

        They default to "nobody deferred this", so every caller that predates deferral
        routing is unaffected — and every caller that DOES have the relationship must
        pass it, whichever side of `bus.deliver` it is on. A row whose origin depends on
        which path filed it is a backlog nobody can query.
        """
        item_id = item_id or db.new_id("bl")
        deps = depends_on or []
        for dep in deps:
            if not self.get_backlog(dep):
                raise KeyError(f"backlog dependency {dep!r} does not exist")
        self.conn.execute(
            "INSERT INTO backlog (id, project, title, description, depends_on, "
            "created_at, origin_wo_id, origin_fo_id, origin_note) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (item_id, project, title, description, db.to_json(deps), db.now(),
             origin_wo_id, origin_fo_id, origin_note),
        )
        return self.get_backlog(item_id)  # type: ignore[return-value]

    def get_backlog(self, item_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM backlog WHERE id=?", (item_id,)).fetchone()
        if not row:
            return None
        d = dict(row)
        d["depends_on"] = db.from_json(d["depends_on"], [])
        return d

    def list_backlog(self, project: str | None = None, status: str | None = "open") -> list[dict[str, Any]]:
        q = "SELECT * FROM backlog"
        conds, params = [], []
        if project:
            conds.append("project=?"); params.append(project)
        if status:
            conds.append("status=?"); params.append(status)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY created_at"
        out = []
        for row in self.conn.execute(q, params).fetchall():
            d = dict(row)
            d["depends_on"] = db.from_json(d["depends_on"], [])
            out.append(d)
        return out

    def unfinished_dependencies(self, item_id: str) -> list[dict[str, Any]]:
        """Dependencies of item that are not yet done (blockers for promotion)."""
        item = self.get_backlog(item_id)
        if not item:
            raise KeyError(f"backlog item {item_id!r} not found")
        blockers = []
        for dep_id in item["depends_on"]:
            dep = self.get_backlog(dep_id)
            if dep is None or dep["status"] != "done":
                blockers.append(dep or {"id": dep_id, "status": "missing", "title": "?"})
        return blockers

    def mark_backlog(self, item_id: str, status: str, promoted_wo_id: str | None = None) -> None:
        assert status in ("open", "promoted", "done", "dropped"), status
        self.conn.execute(
            "UPDATE backlog SET status=?, promoted_wo_id=COALESCE(?, promoted_wo_id) WHERE id=?",
            (status, promoted_wo_id, item_id),
        )

    # -- knowledge -------------------------------------------------------------------

    def add_knowledge(self, content: str, project: str = "", topic: str = "",
                      tags: str = "") -> dict[str, Any]:
        kid = db.new_id("kn")
        self.conn.execute(
            "INSERT INTO knowledge (id, project, ts, topic, content, tags) VALUES (?,?,?,?,?,?)",
            (kid, project, db.now(), topic, content, tags),
        )
        return {"id": kid, "project": project, "topic": topic, "content": content, "tags": tags}

    def retract_knowledge(self, knowledge_id: str, reason: str) -> dict[str, Any]:
        """Retire a knowledge entry the user has superseded. NOT a delete.

        The row stays in the table and keeps being returned by `search_knowledge` — the
        audit trail — while `relevant_knowledge` stops offering it to workers.

        Retracting an already-retired entry RAISES rather than re-stamping it: the
        original reason and timestamp record when the user changed their mind.
        """
        if not reason.strip():
            raise ValueError("a retraction needs a reason: what supersedes this entry?")
        row = self.conn.execute(
            "SELECT * FROM knowledge WHERE id=?", (knowledge_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"knowledge entry {knowledge_id!r} not found")
        if row["retired_at"] is not None:
            raise ValueError(
                f"knowledge entry {knowledge_id!r} was already retired: "
                f"{row['retired_reason']!r}")
        self.conn.execute(
            "UPDATE knowledge SET retired_at=?, retired_reason=? WHERE id=?",
            (db.now(), reason.strip(), knowledge_id),
        )
        return dict(self.conn.execute(
            "SELECT * FROM knowledge WHERE id=?", (knowledge_id,)).fetchone())

    def record_memory_file(self, content: str, project: str = "", topic: str = "",
                           tags: str = MEMORY_TAG) -> bool:
        """Mirror a mirrored-from-a-file memory into the knowledge base.

        A memory file is a living document: the worker rewrites it, so the row is
        replaced rather than appended — otherwise every edit would push older
        learnings out of the recency window with near-duplicates of itself.
        Returns False when nothing changed.

        THE SELECT SKIPS RETIRED ROWS, so the next rewrite of a retracted memory file
        INSERTs a fresh live row instead of writing into the retired one. Replacing a
        retired row in place would turn one retraction into a permanent, silent mute on
        a file the worker keeps updating — every later version written somewhere no
        prompt reads, with no signal to anyone. A retraction is a statement about the
        TEXT that was retired, not about the file (ruled by Neo, question 56). Steady
        state is still at most one live row per (project, topic, tags), plus history.
        """
        row = self.conn.execute(
            "SELECT id, content FROM knowledge WHERE project=? AND topic=? AND tags=?"
            " AND retired_at IS NULL ORDER BY ts DESC LIMIT 1",
            (project, topic, tags),
        ).fetchone()
        if row is not None and row["content"] == content:
            return False
        if row is not None:
            self.conn.execute("UPDATE knowledge SET content=?, ts=? WHERE id=?",
                              (content, db.now(), row["id"]))
            return True
        self.add_knowledge(content, project=project, topic=topic, tags=tags)
        return True

    def relevant_knowledge(self, project: str, limit: int = 8,
                           include_retired: bool = False) -> list[dict[str, Any]]:
        """Project-specific + global entries, most recent first.

        This is the PROMPT feed — `dispatch.build_worker_prompt` offers it to every
        worker — so retired entries are excluded by default. `jarvis learn list` passes
        `include_retired=True` and marks them; `search_knowledge` is the unfiltered
        audit surface.
        """
        retired = "" if include_retired else " AND retired_at IS NULL"
        rows = self.conn.execute(
            f"SELECT * FROM knowledge WHERE (project=? OR project='')"
            f"{retired} ORDER BY ts DESC LIMIT ?",
            (project, limit),
        ).fetchall()
        return db.rows_to_dicts(rows)

    def get_knowledge(self, kid: str) -> dict[str, Any] | None:
        """One entry by id — what an index headline cashes in to. Retired entries are
        returned carrying their retirement metadata; the caller marks them."""
        row = self.conn.execute("SELECT * FROM knowledge WHERE id=?", (kid,)).fetchone()
        return dict(row) if row else None

    def search_knowledge(self, term: str, limit: int = 50, project: str | None = None,
                         topic: str | None = None) -> list[dict[str, Any]]:
        """Free-text search. The AUDIT surface: retired entries are included, carrying
        their `retired_at` and `retired_reason`.

        Also the worker's on-demand retrieval verb, which is why retired rows stay in:
        a worker that looked something up and got nothing back would conclude the OS
        knows nothing about it, when the truth is that it knew and changed its mind.
        The row says which, and `cli.cmd_learn` marks it. `project` scopes to that
        project + global; omit it to search the whole fleet (cross-project learnings
        are often the point).

        **TWO TIERS, and the second is a floor** — see
        docs/superpowers/specs/2026-08-24-ranked-knowledge-search.md §3. First the FTS5
        hits ordered by BM25, which is what buys stemming ("rounding" now finds
        "rounded") and rarity-weighted ranking; then the substring hits FTS5 did not
        return, ordered as they always were — how many of the query's words the row
        matched, then recency. Deduplicated by id, then truncated to `limit`. Nothing
        yesterday's search returned is dropped: §2 measures why pure FTS5 would drop
        some ("deploy" stops finding "deployment", because porter stems the two into
        different buckets). Tier 1 decides what comes FIRST, not what comes back.

        Synonyms remain unsolved and are out of reach for any lexical index: this still
        will not find an entry that only ever says "shipit" (§7, on the backlog).
        """
        rows: list[dict[str, Any]] = []
        seen: set[str] = set()
        for tier in (self._search_fts(term, limit, project, topic),
                     self._search_like(term, limit, project, topic)):
            for row in tier:
                if row["id"] not in seen:
                    seen.add(row["id"])
                    rows.append(row)
        return rows[:limit]

    def _search_fts(self, term: str, limit: int, project: str | None,
                    topic: str | None) -> list[dict[str, Any]]:
        """Tier 1: stemmed, BM25-ranked. Empty on a store without FTS5 (§8)."""
        match = fts_query(term)
        if not self.fts or not match:
            return []
        params: list[Any] = [*FTS_WEIGHTS, match]
        q = ["SELECT k.*, bm25(knowledge_fts, ?, ?, ?) AS _rank FROM knowledge k",
             "JOIN knowledge_fts f ON f.rowid = k.rowid",
             "WHERE knowledge_fts MATCH ?"]
        if project is not None:
            q.append("AND (k.project=? OR k.project='')")
            params.append(project)
        if topic is not None:
            q.append("AND k.topic=?")
            params.append(topic)
        q.append("ORDER BY _rank LIMIT ?")  # bm25 is negative: best match is lowest
        params.append(limit)
        try:
            rows = db.rows_to_dicts(self.conn.execute(" ".join(q), params).fetchall())
        except sqlite3.Error:  # a read verb never raises on what a user typed (§5)
            return []
        for row in rows:  # ranking orders the list; it is not a field of an entry
            row.pop("_rank", None)
        return rows

    def _search_like(self, term: str, limit: int, project: str | None,
                     topic: str | None) -> list[dict[str, Any]]:
        """Tier 2: substring, one point per query word the row matched anywhere.

        Catches what stemming splits apart, and keeps the empty term meaning
        "everything" — the read `jarvis learn list` and the dashboard rely on.
        """
        words = [w for w in (term or "").split() if w] or [""]
        score = " + ".join(
            "(CASE WHEN content LIKE ? OR topic LIKE ? OR tags LIKE ? THEN 1 ELSE 0 END)"
            for _ in words)
        params: list[Any] = []
        for w in words:
            params += [f"%{w}%"] * 3
        q = [f"SELECT *, ({score}) AS _score FROM knowledge WHERE _score > 0"]
        if project is not None:
            q.append("AND (project=? OR project='')")
            params.append(project)
        if topic is not None:
            q.append("AND topic=?")
            params.append(topic)
        q.append("ORDER BY _score DESC, ts DESC LIMIT ?")
        params.append(limit)
        rows = db.rows_to_dicts(self.conn.execute(" ".join(q), params).fetchall())
        for row in rows:
            row.pop("_score", None)
        return rows

    def set_knowledge_tags(self, kid: str, tags: str) -> dict[str, Any] | None:
        self.conn.execute("UPDATE knowledge SET tags=? WHERE id=?", (tags, kid))
        return self.get_knowledge(kid)

    def pin_knowledge(self, kid: str, pinned: bool = True) -> dict[str, Any] | None:
        """Add/remove the `pinned` tag — the switch between 'injected in full into every
        worker prompt' and 'a headline in the index'."""
        row = self.get_knowledge(kid)
        if row is None:
            return None
        tags = split_tags(row["tags"])
        if pinned and PINNED_TAG not in tags:
            tags.append(PINNED_TAG)
        elif not pinned and PINNED_TAG in tags:
            tags.remove(PINNED_TAG)
        return self.set_knowledge_tags(kid, ",".join(tags))

    def count_knowledge(self, project: str, include_retired: bool = False) -> int:
        retired = "" if include_retired else " AND retired_at IS NULL"
        row = self.conn.execute(
            f"SELECT COUNT(*) AS n FROM knowledge WHERE (project=? OR project=''){retired}",
            (project,),
        ).fetchone()
        return int(row["n"])

    def knowledge_topics(self, project: str | None = None,
                         include_retired: bool = False) -> list[tuple[str, int]]:
        """(topic, count) for a project + global entries, biggest topic first.

        Retired entries are excluded by default: this feeds both the prompt's overflow
        roll-call and `jarvis learn topics`, and a topic whose only entries were
        retracted should not advertise itself as somewhere to go looking.
        """
        conds, params = [], []
        if project is not None:
            conds.append("(project=? OR project='')")
            params.append(project)
        if not include_retired:
            conds.append("retired_at IS NULL")
        q = "SELECT topic, COUNT(*) AS n FROM knowledge"
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " GROUP BY topic ORDER BY n DESC, topic"
        return [(r["topic"], int(r["n"]))
                for r in self.conn.execute(q, params).fetchall()]

    def knowledge_brief(self, project: str, pinned_limit: int = 8,
                        digest_limit: int = 40,
                        digest_chars: int = 4000) -> KnowledgeBrief:
        """Build the bounded prompt view of the knowledge base.

        Cost is capped by `pinned_limit` + `digest_chars` no matter how large the base
        grows; what does not fit degrades to a topic roll-call rather than disappearing.

        This is a PROMPT feed, so it inherits `relevant_knowledge`'s rule: retired
        entries never appear. An index headline is still the prompt — retracting a
        ruling has to remove it from the map as well as from the payload, or the worker
        reads the superseded headline and goes looking for the entry behind it.
        """
        brief = KnowledgeBrief(project=project, total=self.count_knowledge(project))
        if brief.total == 0:
            return brief

        rows = db.rows_to_dicts(self.conn.execute(
            "SELECT * FROM knowledge WHERE (project=? OR project='')"
            " AND retired_at IS NULL ORDER BY ts DESC",
            (project,),
        ).fetchall())

        rest: list[dict[str, Any]] = []
        for row in rows:
            if has_tag(row["tags"], PINNED_TAG) and len(brief.pinned) < pinned_limit:
                brief.pinned.append(row)
            else:
                rest.append(row)

        # Selection is round-robin across topics, not straight recency. The index is a
        # map of what the OS knows; letting the one busiest topic consume the whole
        # budget would hide the existence of every other topic — and "I didn't know
        # there was anything to look up" is the exact failure this replaces.
        by_topic: dict[str, list[dict[str, Any]]] = {}
        for row in rest:  # rest is already recency-ordered, so each bucket is too
            by_topic.setdefault(row["topic"], []).append(row)

        selected: list[dict[str, Any]] = []
        spent = 0
        full = False
        while not full and any(by_topic.values()):
            for bucket in by_topic.values():
                if not bucket:
                    continue
                line = headline(bucket[0]["content"])
                if len(selected) >= digest_limit or spent + len(line) > digest_chars:
                    full = True
                    break
                spent += len(line)
                selected.append({**bucket.pop(0), "headline": line})

        # Render grouped: recency picks *what* is shown, topic decides *where* it sits.
        order = {t: i for i, t in enumerate(by_topic)}
        brief.digest = sorted(selected, key=lambda r: (order[r["topic"]], -r["ts"]))
        brief.overflow = sorted(
            ((t, len(rows)) for t, rows in by_topic.items() if rows),
            key=lambda kv: (-kv[1], kv[0]),
        )
        return brief

    # -- who reads the knowledge base --------------------------------------------------

    def record_knowledge_read(self, verb: str, rows: Sequence[dict[str, Any]] = (), *,
                              term: str = "", project: str = "", wo_id: str = "",
                              chars: int | None = None) -> int | None:
        """Write down one retrieval. NEVER RAISES — see `agent_usage`'s closing note.

        An observer that can fail the thing it observes is worse than no observer: a
        worker must not lose a `jarvis learn show` because the OS could not write down
        that it happened. A missing row is visible in the count; a crashed read is not.

        `chars` is what the reader was actually handed, and the caller overrides it when
        that is not the entries' full text: `jarvis learn list` returns headlines unless
        asked for `--full`, and charging it for bodies it never printed would inflate the
        one figure that answers "how much context does memory cost".
        """
        try:
            cur = self.conn.execute(
                "INSERT INTO knowledge_reads (ts, project, wo_id, verb, term, hits, chars)"
                " VALUES (?,?,?,?,?,?,?)",
                (db.now(), project, wo_id, verb, term, len(rows),
                 sum(len(r.get("content") or "") for r in rows)
                 if chars is None else chars),
            )
            read_id = int(cur.lastrowid)
            if verb in AIMED_VERBS:
                self.conn.executemany(
                    "INSERT INTO knowledge_read_hits (read_id, kn_id) VALUES (?,?)",
                    [(read_id, r["id"]) for r in rows if r.get("id")],
                )
            return read_id
        except Exception:  # noqa: BLE001 — accounting never breaks the read it counts
            return None

    def knowledge_reads(self, project: str | None = None, since: float | None = None,
                        limit: int = 2000) -> list[dict[str, Any]]:
        """The raw log, newest first. `project` scopes to reads made FROM that project,
        which is not the same as reads that returned that project's entries: a global
        entry is read from everywhere."""
        conds, params = [], []
        if project:
            conds.append("project=?")
            params.append(project)
        if since is not None:
            conds.append("ts>=?")
            params.append(since)
        where = f" WHERE {' AND '.join(conds)}" if conds else ""
        params.append(limit)
        return db.rows_to_dicts(self.conn.execute(
            f"SELECT * FROM knowledge_reads{where} ORDER BY ts DESC LIMIT ?",
            params).fetchall())

    def knowledge_hit_counts(self, since: float | None = None) -> dict[str, int]:
        """How many aimed reads each entry has answered, by id. Entries never read are
        ABSENT rather than zero — the caller knows the base and can subtract, and a row
        per never-read entry would make the common case the expensive one."""
        q = ("SELECT h.kn_id AS kn_id, COUNT(*) AS n FROM knowledge_read_hits h"
             " JOIN knowledge_reads r ON r.id = h.read_id")
        params: list[Any] = []
        if since is not None:
            q += " WHERE r.ts>=?"
            params.append(since)
        q += " GROUP BY h.kn_id"
        return {r["kn_id"]: int(r["n"]) for r in self.conn.execute(q, params).fetchall()}

    def knowledge_read_summary(self, project: str | None = None,
                               since: float | None = None) -> dict[str, Any]:
        """Totals over the read log: by verb, by who asked, and what came back.

        `misses` counts reads that returned NOTHING. Those are the most informative rows
        in the table and the easiest to lose in an average: an agent asked the base a
        question and the base had no answer, which is a gap in what is recorded, not in
        who reads it.
        """
        conds, params = [], []
        if project:
            conds.append("project=?")
            params.append(project)
        if since is not None:
            conds.append("ts>=?")
            params.append(since)
        where = f" WHERE {' AND '.join(conds)}" if conds else ""
        row = self.conn.execute(
            f"SELECT COUNT(*) AS reads, COALESCE(SUM(chars),0) AS chars,"
            f" COALESCE(SUM(hits),0) AS hits,"
            f" COUNT(DISTINCT CASE WHEN wo_id != '' THEN wo_id END) AS orders,"
            f" SUM(CASE WHEN hits=0 THEN 1 ELSE 0 END) AS misses,"
            f" SUM(CASE WHEN wo_id != '' THEN 1 ELSE 0 END) AS by_workers"
            f" FROM knowledge_reads{where}", params).fetchone()
        out: dict[str, Any] = {k: int(row[k] or 0) for k in
                               ("reads", "chars", "hits", "orders", "misses", "by_workers")}
        out["by_verb"] = {r["verb"]: int(r["n"]) for r in self.conn.execute(
            f"SELECT verb, COUNT(*) AS n FROM knowledge_reads{where}"
            f" GROUP BY verb ORDER BY n DESC", params).fetchall()}
        blank = " AND ".join([*conds, "hits=0", "term != ''"])
        out["unanswered"] = [
            {"verb": r["verb"], "term": r["term"], "wo_id": r["wo_id"], "ts": r["ts"]}
            for r in self.conn.execute(
                f"SELECT verb, term, wo_id, ts FROM knowledge_reads WHERE {blank}"
                f" ORDER BY ts DESC LIMIT 20", params).fetchall()]
        return out

    def knowledge_log_starts(self) -> float | None:
        """When the read log begins, or None if nothing has been recorded yet.

        The boundary every "nobody read this" claim rests on. Work predating it was not
        observed, and counting an unobserved order as one that ignored the knowledge base
        would turn the absence of a measurement into an accusation — the exact failure
        `cost_report` avoids by reporting `found: false` rather than zero.
        """
        row = self.conn.execute("SELECT MIN(ts) AS t FROM knowledge_reads").fetchone()
        return row["t"] if row and row["t"] is not None else None

    def knowledge_reads_by_order(self, since: float | None = None) -> dict[str, int]:
        """How many reads each work order made. The denominator for "did this order
        consult the base at all" lives in the project stores, not here."""
        q = "SELECT wo_id, COUNT(*) AS n FROM knowledge_reads WHERE wo_id != ''"
        params: list[Any] = []
        if since is not None:
            q += " AND ts>=?"
            params.append(since)
        q += " GROUP BY wo_id"
        return {r["wo_id"]: int(r["n"]) for r in self.conn.execute(q, params).fetchall()}

    def knowledge_body_chars(self, project: str | None = None) -> int:
        """Total content characters standing in the base — what the entries WOULD cost
        if they were pasted into a prompt, which is exactly what the index avoids."""
        q = "SELECT COALESCE(SUM(LENGTH(content)),0) AS n FROM knowledge WHERE retired_at IS NULL"
        params: list[Any] = []
        if project:
            q += " AND (project=? OR project='')"
            params.append(project)
        return int(self.conn.execute(q, params).fetchone()["n"])

    # -- the OS's own Claude spend -----------------------------------------------------

    # -- gate rules (what counts as a privileged action; see gate_rules.py) ----

    def _seed_gate_rules(self) -> None:
        """Write the builtin recognisers and canaries, once.

        Guarded by a version key for speed — this runs on every open — but correctness
        rests on the ids, not the guard. They are derived from the rule's content, so an
        insert that has already happened is ignored rather than replayed, and a builtin
        rule the user retracted stays retracted across upgrades and restarts. A random
        id here would silently restore a retired recogniser on every release.
        """
        from . import gate_rules

        if self.get_state("gate_rules_seed") == gate_rules.SEED_VERSION:
            return
        now = db.now()
        for row in gate_rules.seed_rows():
            self.conn.execute(
                """INSERT OR IGNORE INTO gate_rules
                   (id, ts, role, kind, test, pattern, summary, source)
                   VALUES (?,?,?,?,?,?,?,?)""",
                (row["id"], now, row["role"], row["kind"], row["test"], row["pattern"],
                 row.get("summary", ""), row.get("source", "builtin")),
            )
        self.set_state("gate_rules_seed", gate_rules.SEED_VERSION)

    def add_gate_rule(self, role: str, test: str, pattern: str, *, kind: str = "",
                      summary: str = "", source: str = "user", project: str = "",
                      wo_id: str = "", approval_id: int | None = None,
                      reason: str = "", rule_id: str | None = None) -> dict[str, Any]:
        rid = rule_id or db.new_id("gr")
        self.conn.execute(
            """INSERT INTO gate_rules
               (id, ts, role, kind, test, pattern, summary, source, project, wo_id,
                approval_id, reason)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (rid, db.now(), role, kind, test, pattern, summary, source, project, wo_id,
             approval_id, reason),
        )
        return self.get_gate_rule(rid)  # type: ignore[return-value]

    def get_gate_rule(self, rule_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM gate_rules WHERE id=?",
                                (rule_id,)).fetchone()
        return dict(row) if row else None

    def gate_rules(self, role: str | None = None, kind: str | None = None,
                   include_retired: bool = False) -> list[dict[str, Any]]:
        q = "SELECT * FROM gate_rules WHERE 1=1"
        params: list[Any] = []
        if not include_retired:
            q += " AND retired_at IS NULL"
        if role:
            q += " AND role=?"
            params.append(role)
        if kind:
            q += " AND kind=?"
            params.append(kind)
        q += " ORDER BY role, kind, ts"
        return db.rows_to_dicts(self.conn.execute(q, params).fetchall())

    def retract_gate_rule(self, rule_id: str, reason: str) -> dict[str, Any]:
        """Retire a rule without erasing that it was once in force.

        The same shape as `retract_knowledge`, for the same reason: both ledgers are
        append-only, and a rule that turned out to be wrong has to stop applying without
        the record losing what the OS believed and acted on while it did.
        """
        rule = self.get_gate_rule(rule_id)
        if rule is None:
            raise KeyError(f"gate rule {rule_id} not found")
        if rule["retired_at"] is not None:
            raise ValueError(f"gate rule {rule_id} is already retracted")
        self.conn.execute(
            "UPDATE gate_rules SET retired_at=?, retired_reason=? WHERE id=?",
            (db.now(), reason, rule_id),
        )
        return self.get_gate_rule(rule_id)  # type: ignore[return-value]

    # --- the config version ledger -------------------------------------------------
    # docs/superpowers/specs/2026-08-27-the-config-console.md §2, §9.

    def add_config_version(
            self, document: Any, resolved: dict[str, Any], *, actor: str,
            reason: str = "", changes: list[dict[str, Any]] | None = None,
            source_path: str = "",
            schema_version: str | None = None) -> dict[str, Any]:
        """Record a configuration snapshot. Returns the EXISTING row when its id is
        already present, writing nothing.

        That is not an optimisation for duplicate calls — it is the meaning of a
        content-addressed id. An edit that changes nothing is not a change, and
        `jarvis config restore` landing back on the id it restored is the same fact seen
        from the other end. The caller learns which happened from the returned row's
        `ts`/`actor`, not from a flag.

        The id is computed here rather than passed in so no call site can write a row
        whose id does not address its own document.
        """
        from . import bugreport, config_version

        vid = config_version.version_id(document)
        existing = self.get_config_version(vid)
        if existing is not None:
            return existing
        self.conn.execute(
            """INSERT INTO os_config_versions
               (id, ts, actor, reason, schema_version, document_json, resolved_json,
                changes_json, source_path)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (vid, db.now(), actor, reason,
             schema_version if schema_version is not None
             else bugreport.jarvis_version(),
             config_version.canonicalise(document),
             db.to_json(resolved), db.to_json(changes or []), source_path),
        )
        return self.get_config_version(vid)  # type: ignore[return-value]

    def get_config_version(self, version_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM os_config_versions WHERE id=?", (version_id,)).fetchone()
        return self._config_version_row(row)

    def config_versions(self, limit: int = 50) -> list[dict[str, Any]]:
        """Newest first. `rowid` breaks a tie on `ts` so the order is total: two writes
        inside the same clock tick must still have a head."""
        rows = self.conn.execute(
            "SELECT * FROM os_config_versions ORDER BY ts DESC, rowid DESC LIMIT ?",
            (limit,)).fetchall()
        return [self._config_version_row(r) for r in rows]  # type: ignore[misc]

    def head_config_version(self) -> dict[str, Any] | None:
        """What the fleet is configured to run. None on a fleet that never wrote one —
        which reads as "before the console existed", never as version 1."""
        versions = self.config_versions(limit=1)
        return versions[0] if versions else None

    @staticmethod
    def _config_version_row(row: Any) -> dict[str, Any] | None:
        """The raw columns plus the three decoded documents, under names without the
        `_json` suffix. Both are kept: the ledger is append-only and its rendering
        surfaces want the stored bytes, while every caller that USES a version wants the
        objects."""
        if row is None:
            return None
        d = dict(row)
        d["document"] = db.from_json(d["document_json"], {})
        d["resolved"] = db.from_json(d["resolved_json"], {})
        d["changes"] = db.from_json(d["changes_json"], [])
        return d

    def record_gate_rule_hit(self, rule_id: str) -> None:
        """Count an exemption actually clearing a command.

        The counterpart to `dismissed_count()`: that number is what the classifier still
        gets wrong, this one is what it has stopped getting wrong. A learned rule with no
        hits is a rule that generalised nothing.
        """
        self.conn.execute(
            "UPDATE gate_rules SET hits = hits + 1, last_hit=? WHERE id=?",
            (db.now(), rule_id),
        )

    def add_agent_call(self, kind: str, *, project: str = "", wo_id: str = "",
                       label: str = "", model: str = "", question_id: int | None = None,
                       ok: bool = True, session_id: str = "",
                       usage: dict[str, Any] | None = None) -> int:
        """Record one Claude call the OS made itself. See the `agent_calls` schema.

        `usage` is a `claude_cli.derive_turn_usage` envelope, or None for a call that
        produced none (it errored, or the CLI reported nothing). A None-usage row is
        still WORTH WRITING: it says a call was made and cost something unknown, which
        is a different fact from no call at all, and `ok=False` is what tells a reader
        which. Token columns stay zero there, so it cannot inflate a total.
        """
        u = usage or {}
        cur = self.conn.execute(
            """INSERT INTO agent_calls (ts, project, wo_id, kind, label, model,
                                        question_id, ok, session_id, cost_usd, input,
                                        cache_write, cache_read, output, usage_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (db.now(), project, wo_id, kind, label, model, question_id, 1 if ok else 0,
             session_id,
             u.get("total_cost_usd"), u.get("input") or 0, u.get("cache_write") or 0,
             u.get("cache_read") or 0, u.get("output") or 0,
             db.to_json(usage) if usage else None),
        )
        return int(cur.lastrowid or 0)

    def agent_calls(self, wo_id: str | None = None, project: str | None = None,
                    limit: int = 500) -> list[dict[str, Any]]:
        """The OS's calls, newest first — for one work order, one project, or all."""
        where, params = [], []
        if wo_id is not None:
            where.append("wo_id=?")
            params.append(wo_id)
        if project is not None:
            where.append("project=?")
            params.append(project)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        return db.rows_to_dicts(self.conn.execute(
            f"SELECT * FROM agent_calls {clause} ORDER BY ts DESC LIMIT ?",
            (*params, limit)).fetchall())

    def agent_call_totals(self, project: str | None = None) -> list[dict[str, Any]]:
        """Every work order's recorded spend, summed in SQL, grouped by kind/label/model.

        Grouped rather than flat because every consumer needs the grouping: the report
        prices each group at its own model's list rate (a digest on Haiku is not Opus
        waste), and the per-work-order view shows what the spend went ON — five panel
        seats reads very differently from one Neo answer.

        `label` joins the key so the WORKER-SUBPROCESS class can be broken down by what
        ran the calls ("pytest: 40 calls") without a second query and, more importantly,
        without a row limit: this is a sum, and a truncated sum understates exactly the
        expensive work order someone is investigating. Consumers that only want kind and
        model re-aggregate in Python, so the finer key costs them nothing.

        One query for the whole fleet: the alternative is a query per work order, and
        the cost report walks every work order there is.

        THE TTL SPLIT COMES OUT OF `usage_json`, not out of a column, and summing it here
        is what stops the report under-pricing its own overhead at the 1.25x floor (spec:
        2026-08-22-the-five-minute-write-everywhere.md). `json_extract` returns NULL both
        for a row with no envelope and for one whose envelope predates the field, and
        COALESCE folds both into the same honest zero: no split known, floor rate.
        """
        clause = "WHERE project=?" if project else ""
        params = (project,) if project else ()
        return db.rows_to_dicts(self.conn.execute(
            f"""SELECT wo_id, kind, label, model, COUNT(*) AS calls,
                       SUM(cost_usd) AS cost_usd, SUM(input) AS input,
                       SUM(cache_write) AS cache_write, SUM(cache_read) AS cache_read,
                       SUM(output) AS output, SUM(1 - ok) AS failed,
                       SUM(COALESCE(json_extract(usage_json, '$.cache_1h'), 0))
                           AS cache_1h,
                       SUM(COALESCE(json_extract(usage_json, '$.cache_5m'), 0))
                           AS cache_5m
                FROM agent_calls {clause}
                GROUP BY wo_id, kind, label, model""", params).fetchall())

    # -- os state ----------------------------------------------------------------------

    def set_state(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO os_state (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def get_state(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM os_state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None
