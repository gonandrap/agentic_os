"""Per-project store: <project>/.jarvis/jarvis.db

Authoritative record of a project's work orders, their event timeline, the user⇄agent
message queue, the notification outbox, and assumptions pending review.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import db
from .paths import project_db_path

# Work order lifecycle.
WO_STATUSES = (
    "pending",       # created, waiting for the project orchestrator to pick it up
    "dispatching",   # claimed by the daemon, worker being spawned
    "running",       # worker session active
    "waiting_input", # worker asked something / is blocked on the user
    "needs_review",  # finished but has pending assumptions or attention items
    "completed",
    "failed",
    "cancelled",
)
OPEN_STATUSES = ("pending", "dispatching", "running", "waiting_input", "needs_review")

# How the work order entered the system. jarvis/ui follow the framework; manual is a
# direct DB insert; adhoc is a background session we discovered that Jarvis didn't spawn.
WO_ORIGINS = ("jarvis", "ui", "manual", "adhoc")

SCHEMA = """
CREATE TABLE IF NOT EXISTS work_orders (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    origin TEXT NOT NULL DEFAULT 'manual',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    model TEXT,
    effort TEXT,
    permission_mode TEXT,
    append_system_prompt TEXT,
    session_id TEXT,
    bg_id TEXT,
    worktree TEXT,
    branch TEXT,
    needs_attention INTEGER NOT NULL DEFAULT 0,
    attention_reason TEXT,
    result_summary TEXT,
    backlog_id TEXT,
    metadata TEXT
);
CREATE TABLE IF NOT EXISTS wo_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wo_id TEXT NOT NULL REFERENCES work_orders(id),
    ts REAL NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT
);
CREATE TABLE IF NOT EXISTS wo_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wo_id TEXT NOT NULL REFERENCES work_orders(id),
    ts REAL NOT NULL,
    direction TEXT NOT NULL,            -- user_to_agent | agent_to_user
    content TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'jarvis',  -- jarvis | ui | direct
    status TEXT NOT NULL DEFAULT 'queued',  -- queued | delivered | failed
    delivered_at REAL
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    level TEXT NOT NULL DEFAULT 'info', -- info | warning | critical
    title TEXT NOT NULL,
    body TEXT NOT NULL DEFAULT '',
    wo_id TEXT,
    source TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'new'  -- new | routed
);
CREATE TABLE IF NOT EXISTS assumptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wo_id TEXT NOT NULL REFERENCES work_orders(id),
    ts REAL NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' -- pending | accepted | rejected
);
CREATE INDEX IF NOT EXISTS idx_wo_status ON work_orders(status);
CREATE INDEX IF NOT EXISTS idx_events_wo ON wo_events(wo_id);
CREATE INDEX IF NOT EXISTS idx_msgs_status ON wo_messages(status);
CREATE INDEX IF NOT EXISTS idx_notif_status ON notifications(status);
"""


class ProjectStore:
    def __init__(self, project_path: str | Path):
        self.project_path = Path(project_path)
        self.db_path = project_db_path(self.project_path)
        self.conn = db.connect(self.db_path)
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # -- work orders -------------------------------------------------------

    def create_work_order(
        self,
        title: str,
        description: str = "",
        origin: str = "jarvis",
        model: str | None = None,
        effort: str | None = None,
        permission_mode: str | None = None,
        append_system_prompt: str | None = None,
        backlog_id: str | None = None,
        metadata: dict[str, Any] | None = None,
        wo_id: str | None = None,
    ) -> dict[str, Any]:
        assert origin in WO_ORIGINS, origin
        wo_id = wo_id or db.new_id("wo")
        ts = db.now()
        self.conn.execute(
            """INSERT INTO work_orders (id, title, description, status, origin,
                   created_at, updated_at, model, effort, permission_mode,
                   append_system_prompt, backlog_id, metadata)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                wo_id, title, description, "pending", origin, ts, ts, model, effort,
                permission_mode, append_system_prompt, backlog_id,
                db.to_json(metadata or {}),
            ),
        )
        self.add_event(wo_id, "created", {"origin": origin})
        return self.get_work_order(wo_id)

    def get_work_order(self, wo_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM work_orders WHERE id=?", (wo_id,)).fetchone()
        if row is None:
            raise KeyError(f"work order {wo_id!r} not found in {self.db_path}")
        return dict(row)

    def find_by_session(self, session_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM work_orders WHERE session_id=?", (session_id,)
        ).fetchone()
        return dict(row) if row else None

    def list_work_orders(
        self, statuses: tuple[str, ...] | None = None, limit: int = 200
    ) -> list[dict[str, Any]]:
        if statuses:
            q = ",".join("?" for _ in statuses)
            rows = self.conn.execute(
                f"SELECT * FROM work_orders WHERE status IN ({q}) ORDER BY created_at DESC LIMIT ?",
                (*statuses, limit),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM work_orders ORDER BY created_at DESC LIMIT ?", (limit,)
            ).fetchall()
        return db.rows_to_dicts(rows)

    def claim_next_pending(self) -> dict[str, Any] | None:
        """Atomically claim the oldest pending order (pending -> dispatching)."""
        cur = self.conn.execute(
            """UPDATE work_orders SET status='dispatching', updated_at=?
               WHERE id = (SELECT id FROM work_orders WHERE status='pending'
                           ORDER BY created_at LIMIT 1)
               RETURNING *"""
            , (db.now(),),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def count_active(self) -> int:
        row = self.conn.execute(
            "SELECT COUNT(*) c FROM work_orders WHERE status IN ('dispatching','running','waiting_input')"
        ).fetchone()
        return row["c"]

    def update_work_order(self, wo_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = db.now()
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(
            f"UPDATE work_orders SET {cols} WHERE id=?", (*fields.values(), wo_id)
        )

    def set_status(self, wo_id: str, status: str, **extra: Any) -> None:
        assert status in WO_STATUSES, status
        self.update_work_order(wo_id, status=status, **extra)
        self.add_event(wo_id, "status", {"status": status})

    def flag_attention(self, wo_id: str, reason: str) -> None:
        self.update_work_order(wo_id, needs_attention=1, attention_reason=reason)
        self.add_event(wo_id, "attention", {"reason": reason})

    def clear_attention(self, wo_id: str) -> None:
        self.update_work_order(wo_id, needs_attention=0, attention_reason=None)

    # -- events --------------------------------------------------------------

    def add_event(self, wo_id: str, kind: str, payload: dict[str, Any] | None = None) -> None:
        self.conn.execute(
            "INSERT INTO wo_events (wo_id, ts, kind, payload) VALUES (?,?,?,?)",
            (wo_id, db.now(), kind, db.to_json(payload or {})),
        )

    def list_events(self, wo_id: str, limit: int = 200) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM wo_events WHERE wo_id=? ORDER BY ts LIMIT ?", (wo_id, limit)
        ).fetchall()
        return db.rows_to_dicts(rows)

    # -- messages (user feedback queue) ---------------------------------------

    def queue_message(self, wo_id: str, content: str, source: str = "jarvis",
                      direction: str = "user_to_agent", status: str = "queued") -> int:
        cur = self.conn.execute(
            "INSERT INTO wo_messages (wo_id, ts, direction, content, source, status) VALUES (?,?,?,?,?,?)",
            (wo_id, db.now(), direction, content, source, status),
        )
        return int(cur.lastrowid)

    def queued_messages(self, wo_id: str | None = None) -> list[dict[str, Any]]:
        if wo_id:
            rows = self.conn.execute(
                "SELECT * FROM wo_messages WHERE status='queued' AND direction='user_to_agent' AND wo_id=? ORDER BY ts",
                (wo_id,),
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM wo_messages WHERE status='queued' AND direction='user_to_agent' ORDER BY ts"
            ).fetchall()
        return db.rows_to_dicts(rows)

    def mark_message(self, msg_id: int, status: str) -> None:
        self.conn.execute(
            "UPDATE wo_messages SET status=?, delivered_at=? WHERE id=?",
            (status, db.now() if status == "delivered" else None, msg_id),
        )

    def list_messages(self, wo_id: str, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM wo_messages WHERE wo_id=? ORDER BY ts LIMIT ?", (wo_id, limit)
        ).fetchall()
        return db.rows_to_dicts(rows)

    # -- notifications outbox --------------------------------------------------

    def add_notification(self, title: str, body: str = "", level: str = "info",
                         wo_id: str | None = None, source: str = "") -> int:
        assert level in ("info", "warning", "critical"), level
        cur = self.conn.execute(
            "INSERT INTO notifications (ts, level, title, body, wo_id, source) VALUES (?,?,?,?,?,?)",
            (db.now(), level, title, body, wo_id, source),
        )
        return int(cur.lastrowid)

    def unrouted_notifications(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM notifications WHERE status='new' ORDER BY ts"
        ).fetchall()
        return db.rows_to_dicts(rows)

    def mark_notification_routed(self, notif_id: int) -> None:
        self.conn.execute("UPDATE notifications SET status='routed' WHERE id=?", (notif_id,))

    # -- assumptions -----------------------------------------------------------

    def add_assumption(self, wo_id: str, content: str) -> int:
        cur = self.conn.execute(
            "INSERT INTO assumptions (wo_id, ts, content) VALUES (?,?,?)",
            (wo_id, db.now(), content),
        )
        self.add_event(wo_id, "assumption", {"content": content})
        return int(cur.lastrowid)

    def pending_assumptions(self, wo_id: str | None = None) -> list[dict[str, Any]]:
        if wo_id:
            rows = self.conn.execute(
                "SELECT * FROM assumptions WHERE status='pending' AND wo_id=? ORDER BY ts", (wo_id,)
            ).fetchall()
        else:
            rows = self.conn.execute(
                "SELECT * FROM assumptions WHERE status='pending' ORDER BY ts"
            ).fetchall()
        return db.rows_to_dicts(rows)

    def review_assumption(self, assumption_id: int, status: str) -> None:
        assert status in ("accepted", "rejected"), status
        self.conn.execute("UPDATE assumptions SET status=? WHERE id=?", (status, assumption_id))

    # -- summary ----------------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        by_status = {
            r["status"]: r["c"]
            for r in self.conn.execute(
                "SELECT status, COUNT(*) c FROM work_orders GROUP BY status"
            ).fetchall()
        }
        attention = self.conn.execute(
            "SELECT COUNT(*) c FROM work_orders WHERE needs_attention=1"
        ).fetchone()["c"]
        pending_assumptions = self.conn.execute(
            "SELECT COUNT(*) c FROM assumptions WHERE status='pending'"
        ).fetchone()["c"]
        return {
            "by_status": by_status,
            "needs_attention": attention,
            "pending_assumptions": pending_assumptions,
        }
