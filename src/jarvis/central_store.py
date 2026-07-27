"""Central store: $JARVIS_HOME/os.db

Holds everything that must be unified across projects: the project registry, the
notification inbox, the backlog (with dependencies), and the knowledge base.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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


def split_tags(tags: str) -> list[str]:
    return [t for t in (s.strip() for s in (tags or "").split(",")) if t]


def has_tag(tags: str, tag: str) -> bool:
    return tag in split_tags(tags)


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
    created_at REAL NOT NULL
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
CREATE INDEX IF NOT EXISTS idx_inbox_status ON inbox(status);
CREATE INDEX IF NOT EXISTS idx_backlog_project ON backlog(project, status);
"""

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
}


class CentralStore:
    def __init__(self, path: Path | None = None):
        ensure_home()
        self.db_path = path or central_db_path()
        self.conn = db.connect(self.db_path)
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        for table, columns in ADDED_COLUMNS.items():
            have = {
                r["name"]
                for r in self.conn.execute(f"PRAGMA table_info({table})").fetchall()
            }
            for name, decl in columns.items():
                if name not in have:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")

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
        """
        inbox = self.conn.execute("DELETE FROM inbox WHERE wo_id=?", (wo_id,)).rowcount
        backlog = self.conn.execute(
            """UPDATE backlog SET status='open', promoted_wo_id=NULL
               WHERE promoted_wo_id=? AND status='promoted'""",
            (wo_id,),
        ).rowcount
        return {"inbox": inbox, "backlog_reopened": backlog}

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
                    depends_on: list[str] | None = None, item_id: str | None = None) -> dict[str, Any]:
        item_id = item_id or db.new_id("bl")
        deps = depends_on or []
        for dep in deps:
            if not self.get_backlog(dep):
                raise KeyError(f"backlog dependency {dep!r} does not exist")
        self.conn.execute(
            "INSERT INTO backlog (id, project, title, description, depends_on, created_at) VALUES (?,?,?,?,?,?)",
            (item_id, project, title, description, db.to_json(deps), db.now()),
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
        """
        like = f"%{term}%"
        q = ["SELECT * FROM knowledge WHERE (content LIKE ? OR topic LIKE ? OR tags LIKE ?)"]
        params: list[Any] = [like, like, like]
        if project is not None:
            q.append("AND (project=? OR project='')")
            params.append(project)
        if topic is not None:
            q.append("AND topic=?")
            params.append(topic)
        q.append("ORDER BY ts DESC LIMIT ?")
        params.append(limit)
        return db.rows_to_dicts(self.conn.execute(" ".join(q), params).fetchall())

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

    # -- os state ----------------------------------------------------------------------

    def set_state(self, key: str, value: str) -> None:
        self.conn.execute(
            "INSERT INTO os_state (key, value) VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, value),
        )

    def get_state(self, key: str) -> str | None:
        row = self.conn.execute("SELECT value FROM os_state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else None
