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
CREATE INDEX IF NOT EXISTS idx_gate_rules_role ON gate_rules(role, kind);
CREATE INDEX IF NOT EXISTS idx_inbox_status ON inbox(status);
CREATE INDEX IF NOT EXISTS idx_backlog_project ON backlog(project, status);
CREATE INDEX IF NOT EXISTS idx_agent_calls_wo ON agent_calls(wo_id, ts);
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
        backlog = self.conn.execute(
            """UPDATE backlog SET status='open', promoted_wo_id=NULL
               WHERE promoted_wo_id=? AND status='promoted'""",
            (wo_id,),
        ).rowcount
        return {"inbox": inbox, "agent_calls": calls, "backlog_reopened": backlog}

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

        **Words are ORed and the result is ranked by how many of them a row matched**,
        rather than the whole term being one `LIKE '%…%'`. A single word behaves exactly
        as it always did; the difference is a query like "cents rounding format", which
        under phrase matching required that literal string and so returned nothing at
        all. That is how real agents search — the retrieval eval
        (evals/llm/test_knowledge_retrieval_judgment.py) scored 2/7 on phrase matching
        purely because natural multi-word queries retrieved nothing — and an index whose
        lookup verb only answers single keywords is not a lookup verb.

        Still substring matching per word, so it has no stemming and no synonyms:
        "rounding" does not find "rounded" on its own, it survives only by riding along
        with the other words in the query. FTS5 is the real fix and is on the backlog.
        """
        words = [w for w in (term or "").split() if w] or [""]
        # score = how many of the query's words this row matched anywhere
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
        for row in rows:  # ranking is how the list is ordered, not a field of an entry
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
