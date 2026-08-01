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
# Settled: nothing more will happen to these on their own. They are the bulk of an old
# project's history, so listings collapse them behind a count rather than printing them.
TERMINAL_STATUSES = ("completed", "cancelled", "failed")

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
    -- LEGACY (background-session transport), no longer written; see wo_turns. A
    -- non-NULL job_id is now only a marker that this work order predates headless
    -- turns, which is what tells worker_session to release its background agent
    -- before the next turn resumes.
    job_id TEXT,
    reply_job_id TEXT,
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
-- Requests to perform a privileged action (merge a PR, cut a release). Filed by the
-- PreToolUse gate when a worker attempts one, reviewed by Neo, and consumed by the
-- retry. A row is a receipt for one command, not a capability: see gates.py.
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wo_id TEXT NOT NULL REFERENCES work_orders(id),
    ts REAL NOT NULL,
    kind TEXT NOT NULL,                     -- gates.KIND_NAMES
    command TEXT NOT NULL,                  -- the exact string the grant authorises
    matched TEXT NOT NULL DEFAULT '',       -- the recogniser that fired
    justification TEXT NOT NULL DEFAULT '',
    evidence TEXT NOT NULL DEFAULT '',
    -- pending | approved | denied | dismissed | expired.
    -- `dismissed` is not a verdict on a privileged action, it is a verdict on the
    -- CLASSIFIER: the command never performed one and the gate matched it by mistake.
    -- It clears the command like an approval does but records no authorisation, and it
    -- is the only decided status that never expires — see gates.py.
    status TEXT NOT NULL DEFAULT 'pending',
    -- Neo declined to decide, so the request is still pending but it is now the USER
    -- who holds it. This is the bit that decides whether a gate costs the user any
    -- attention: pending-with-Neo must stay silent, pending-with-user must not.
    escalated INTEGER NOT NULL DEFAULT 0,
    escalation_reason TEXT,
    neo_question_id INTEGER,
    decided_by TEXT,                        -- neo | user
    decision_reason TEXT,
    decided_at REAL,
    expires_at REAL,
    uses INTEGER NOT NULL DEFAULT 0,
    max_uses INTEGER NOT NULL DEFAULT 3
);
-- One turn of a worker's conversation: a `claude -p` process Jarvis started, and what
-- it said back. The work order's conversation IS this table in order of `seq`.
--
-- Replaces the old job_id/reply_job_id pair, which tracked a background job through the
-- Claude supervisor's private state file. Owning the record outright is what makes reply
-- capture a field read instead of a retry loop over someone else's internals.
CREATE TABLE IF NOT EXISTS wo_turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wo_id TEXT NOT NULL REFERENCES work_orders(id),
    seq INTEGER NOT NULL,                   -- 1-based position in the conversation
    kind TEXT NOT NULL,                     -- dispatch | message
    msg_id INTEGER,                         -- the wo_messages row that triggered it
    prompt TEXT NOT NULL,
    state TEXT NOT NULL DEFAULT 'running',  -- running | done | failed
    pid INTEGER,
    started_at REAL NOT NULL,
    ended_at REAL,
    exit_code INTEGER,
    result TEXT,                            -- the turn's final assistant message
    error TEXT,
    cost_usd REAL,
    num_turns INTEGER,
    outfile TEXT NOT NULL DEFAULT '',
    errfile TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_turns_wo ON wo_turns(wo_id, seq);
CREATE INDEX IF NOT EXISTS idx_turns_state ON wo_turns(state);
CREATE INDEX IF NOT EXISTS idx_wo_status ON work_orders(status);
CREATE INDEX IF NOT EXISTS idx_events_wo ON wo_events(wo_id);
CREATE INDEX IF NOT EXISTS idx_msgs_status ON wo_messages(status);
CREATE INDEX IF NOT EXISTS idx_notif_status ON notifications(status);
CREATE INDEX IF NOT EXISTS idx_approvals_wo ON approvals(wo_id, status);
"""

# Columns added after the first release. `CREATE TABLE IF NOT EXISTS` is a no-op on an
# existing database, so new columns must be ALTERed in on open.
ADDED_COLUMNS = {
    "work_orders": {
        "job_id": "TEXT",
        "reply_job_id": "TEXT",
        # Hidden orders stay on the record but stop competing for the user's attention:
        # out of listings, out of the summary, and never dispatched.
        "hidden": "INTEGER NOT NULL DEFAULT 0",
        # Blockers the user has explicitly seen and dismissed (JSON list). Attention is
        # re-derived from state on every reconcile tick, so clearing the flag alone does
        # not stick — the tick puts it straight back. This is what makes an ack hold:
        # `true_blockers` subtracts these, and only these, so a *new* blocker still
        # surfaces. Cleared whenever the flag legitimately drops (the ack is spent).
        "acknowledged_blockers": "TEXT",
        # LEGACY, no longer written. Under the old background-session transport every
        # delivered turn forked a fresh session id, so a work order accumulated a trail
        # of spent ones and needed this to stop its binding walking backwards. Headless
        # turns reuse one Jarvis-minted id for the work order's whole life, so there is
        # no trail to keep. Retained because old rows still carry their history.
        "prior_sessions": "TEXT",
    },
}


class ProjectStore:
    def __init__(self, project_path: str | Path):
        self.project_path = Path(project_path)
        self.db_path = project_db_path(self.project_path)
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
        self, statuses: tuple[str, ...] | None = None, limit: int = 200,
        include_hidden: bool = False,
    ) -> list[dict[str, Any]]:
        conds, params = [], []
        if statuses:
            conds.append(f"status IN ({','.join('?' for _ in statuses)})")
            params.extend(statuses)
        if not include_hidden:
            conds.append("hidden=0")
        where = f" WHERE {' AND '.join(conds)}" if conds else ""
        rows = self.conn.execute(
            f"SELECT * FROM work_orders{where} ORDER BY created_at DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        return db.rows_to_dicts(rows)

    def status_counts(self, include_hidden: bool = False) -> dict[str, int]:
        """How many work orders sit in each status. Counted in SQL, not by listing.

        `list_work_orders` is capped at `limit`, so counting its result would quietly
        under-report exactly where it matters most — a project with more history than
        one page of it.
        """
        where = "" if include_hidden else " WHERE hidden=0"
        rows = self.conn.execute(
            f"SELECT status, COUNT(*) AS n FROM work_orders{where} GROUP BY status"
        ).fetchall()
        return {row["status"]: int(row["n"]) for row in rows}

    def claim_next_pending(self) -> dict[str, Any] | None:
        """Atomically claim the oldest pending order (pending -> dispatching)."""
        cur = self.conn.execute(
            """UPDATE work_orders SET status='dispatching', updated_at=?
               WHERE id = (SELECT id FROM work_orders
                           WHERE status='pending' AND hidden=0
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
        # The blocker is gone, so any ack against it is spent: if the same reason comes
        # back later it is a new event and must be shown again.
        self.update_work_order(wo_id, needs_attention=0, attention_reason=None,
                               acknowledged_blockers=None)

    def ack_attention(self, wo_id: str, blockers: list[str]) -> None:
        """Record that the user has seen these blockers, and put the flag down.

        Deliberately dumb: the caller derives `blockers` (via `invariants.true_blockers`)
        so the store stays free of policy. Unlike `clear_attention` this remembers what
        was dismissed, which is the only reason the flag stays down across reconcile
        ticks — see `acknowledged_blockers` in ADDED_COLUMNS.
        """
        self.update_work_order(wo_id, needs_attention=0, attention_reason=None,
                               acknowledged_blockers=db.to_json(blockers))
        self.add_event(wo_id, "acknowledged", {"blockers": blockers})

    def set_hidden(self, wo_id: str, hidden: bool = True) -> None:
        """Hide (or unhide) a work order.

        Hiding is non-destructive: the record and its whole history stay, they just
        stop showing up in listings, summaries and the attention list, and a hidden
        pending order is never dispatched.
        """
        self.get_work_order(wo_id)  # KeyError if it doesn't exist
        self.update_work_order(wo_id, hidden=1 if hidden else 0)
        self.add_event(wo_id, "hidden", {"hidden": bool(hidden)})

    def delete_work_order(self, wo_id: str) -> dict[str, int]:
        """Erase a work order and everything hanging off it. Returns the row counts.

        Foreign keys are enforced (see db.connect), so children go first. The whole
        cascade runs in one transaction: a half-deleted work order is worse than none.
        """
        self.get_work_order(wo_id)  # KeyError if it doesn't exist
        deleted: dict[str, int] = {}
        self.conn.execute("BEGIN")
        try:
            for key, table in (("events", "wo_events"), ("messages", "wo_messages"),
                               ("turns", "wo_turns"),
                               ("assumptions", "assumptions"),
                               ("approvals", "approvals"),
                               ("notifications", "notifications")):
                cur = self.conn.execute(f"DELETE FROM {table} WHERE wo_id=?", (wo_id,))
                deleted[key] = cur.rowcount
            self.conn.execute("DELETE FROM work_orders WHERE id=?", (wo_id,))
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return deleted

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

    def record_agent_reply(self, wo_id: str, content: str, source: str = "worker") -> int:
        """Persist a worker's final assistant message into the work order record.

        The work order is the representation of the worker's conversation: the user and
        Neo decide from it and never open the session, so the full reply is stored, not
        just the `wo finish --summary` headline.
        """
        return self.queue_message(wo_id, content, source=source,
                                  direction="agent_to_user", status="delivered")

    def agent_replies(self, wo_id: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM wo_messages WHERE wo_id=? AND direction='agent_to_user' ORDER BY ts",
            (wo_id,),
        ).fetchall()
        return db.rows_to_dicts(rows)

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

    # -- turns (the worker's conversation) --------------------------------------

    def create_turn(self, wo_id: str, kind: str, prompt: str,
                    msg_id: int | None = None, outfile: str = "",
                    errfile: str = "") -> dict[str, Any]:
        """Open a turn row. Written BEFORE the process is spawned, so a turn can never
        be running with nothing on record to reap it."""
        assert kind in ("dispatch", "message"), kind
        seq = self.conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM wo_turns WHERE wo_id=?", (wo_id,)
        ).fetchone()["n"]
        cur = self.conn.execute(
            """INSERT INTO wo_turns (wo_id, seq, kind, msg_id, prompt, started_at,
                                     outfile, errfile)
               VALUES (?,?,?,?,?,?,?,?)""",
            (wo_id, seq, kind, msg_id, prompt, db.now(), outfile, errfile),
        )
        return self.get_turn(int(cur.lastrowid))  # type: ignore[arg-type,return-value]

    def get_turn(self, turn_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM wo_turns WHERE id=?", (turn_id,)).fetchone()
        return dict(row) if row else None

    def set_turn_pid(self, turn_id: int, pid: int) -> None:
        self.conn.execute("UPDATE wo_turns SET pid=? WHERE id=?", (pid, turn_id))

    def finish_turn(self, turn_id: int, state: str, result: str | None = None,
                    error: str | None = None, cost_usd: float | None = None,
                    num_turns: int | None = None) -> dict[str, Any]:
        assert state in ("done", "failed"), state
        self.conn.execute(
            """UPDATE wo_turns SET state=?, ended_at=?, result=?, error=?, cost_usd=?,
                                   num_turns=? WHERE id=?""",
            (state, db.now(), result, error, cost_usd, num_turns, turn_id),
        )
        return self.get_turn(turn_id)  # type: ignore[return-value]

    def latest_turn(self, wo_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM wo_turns WHERE wo_id=? ORDER BY seq DESC LIMIT 1", (wo_id,)
        ).fetchone()
        return dict(row) if row else None

    def running_turns(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM wo_turns WHERE state='running' ORDER BY started_at"
        ).fetchall()
        return db.rows_to_dicts(rows)

    def list_turns(self, wo_id: str, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM wo_turns WHERE wo_id=? ORDER BY seq LIMIT ?", (wo_id, limit)
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
            # Fleet-wide view: assumptions of hidden work orders aren't asking for review.
            rows = self.conn.execute(
                """SELECT a.* FROM assumptions a JOIN work_orders w ON w.id = a.wo_id
                   WHERE a.status='pending' AND w.hidden=0 ORDER BY a.ts"""
            ).fetchall()
        return db.rows_to_dicts(rows)

    def all_assumptions(self, wo_id: str) -> list[dict[str, Any]]:
        """Every assumption of a work order, reviewed or not.

        `pending_assumptions` answers "what does the user still owe a decision on";
        this answers "what was ever recorded", which is what the persistence invariant
        has to compare the timeline against.
        """
        rows = self.conn.execute(
            "SELECT * FROM assumptions WHERE wo_id=? ORDER BY ts", (wo_id,)
        ).fetchall()
        return db.rows_to_dicts(rows)

    def review_assumption(self, assumption_id: int, status: str) -> None:
        assert status in ("accepted", "rejected"), status
        self.conn.execute("UPDATE assumptions SET status=? WHERE id=?", (status, assumption_id))

    # -- approvals (privileged-action gates; see gates.py) ------------------------

    def add_approval(self, wo_id: str, kind: str, command: str, matched: str = "",
                     justification: str = "", evidence: str = "",
                     max_uses: int = 3) -> dict[str, Any]:
        cur = self.conn.execute(
            """INSERT INTO approvals (wo_id, ts, kind, command, matched, justification,
                                      evidence, max_uses)
               VALUES (?,?,?,?,?,?,?,?)""",
            (wo_id, db.now(), kind, command, matched, justification, evidence, max_uses),
        )
        approval_id = int(cur.lastrowid)  # type: ignore[arg-type]
        self.add_event(wo_id, "gate_requested", {
            "approval_id": approval_id, "kind": kind, "command": command,
        })
        return self.get_approval(approval_id)  # type: ignore[return-value]

    def get_approval(self, approval_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        return dict(row) if row else None

    def latest_approval_for(self, wo_id: str, kind: str, command: str
                            ) -> dict[str, Any] | None:
        """The most recent request this work order filed for this exact command.

        Exact-match on the command is the whole security model: a grant is a receipt for
        one string. A worker that "tidies up" the command on retry gets a fresh gate,
        which is the correct outcome — the reviewer approved what it read.
        """
        row = self.conn.execute(
            """SELECT * FROM approvals WHERE wo_id=? AND kind=? AND command=?
               ORDER BY ts DESC, id DESC LIMIT 1""",
            (wo_id, kind, command),
        ).fetchone()
        return dict(row) if row else None

    def link_neo_question(self, approval_id: int, question_id: int) -> None:
        self.conn.execute("UPDATE approvals SET neo_question_id=? WHERE id=?",
                          (question_id, approval_id))

    def approval_for_question(self, question_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM approvals WHERE neo_question_id=?",
                                (question_id,)).fetchone()
        return dict(row) if row else None

    def mark_approval_escalated(self, approval_id: int, reason: str) -> None:
        """Neo declined to decide: the request stays open, now against the user."""
        self.conn.execute(
            "UPDATE approvals SET escalated=1, escalation_reason=? WHERE id=?",
            (reason, approval_id),
        )

    def escalated_approvals(self, wo_id: str | None = None) -> list[dict[str, Any]]:
        """Requests waiting on the user specifically — the only gates that are allowed
        to consume attention."""
        return [a for a in self.pending_approvals(wo_id) if a["escalated"]]

    def decide_approval(self, approval_id: int, verdict: str, reason: str,
                        decided_by: str, ttl_seconds: int = 3600) -> dict[str, Any]:
        """Record a verdict — `approved`, `denied` or `dismissed`.

        Only an approval gets an expiry. It starts its clock now, not when the request
        was filed: the window exists to bound the gap between "yes" and the act.

        A dismissal deliberately gets none. It does not say "you may do this for the next
        hour", it says "this command performs no privileged action" — a fact about the
        command string, which does not lapse. Giving it a TTL would model it as a
        permission, and would make one classifier bug cost a second review an hour later.
        """
        if verdict not in ("approved", "denied", "dismissed"):
            raise ValueError(f"unknown verdict {verdict!r}")
        approval = self.get_approval(approval_id)
        if approval is None:
            raise KeyError(f"approval {approval_id} not found")
        now = db.now()
        self.conn.execute(
            """UPDATE approvals SET status=?, decided_by=?, decision_reason=?,
                                    decided_at=?, expires_at=? WHERE id=?""",
            (verdict, decided_by, reason, now,
             now + ttl_seconds if verdict == "approved" else None, approval_id),
        )
        return self.get_approval(approval_id)  # type: ignore[return-value]

    def usable_grant(self, wo_id: str, kind: str, command: str) -> dict[str, Any] | None:
        """The decided request that lets this exact command through right now, or None.

        Two statuses clear a command, and they are not the same thing — callers must read
        `status` to tell them apart, because only one of them is an authorisation:

        * `approved` — a privileged action was reviewed and permitted. Bounded: expiry
          and use-count are re-checked here rather than trusted from the row, so a grant
          cannot outlive its window just because nothing swept the table.
        * `dismissed` — the gate matched a command that performs no privileged action.
          Unbounded on purpose (see `decide_approval`); the scope that keeps it safe is
          the exact-string match on one work order, not a clock.
        """
        approval = self.latest_approval_for(wo_id, kind, command)
        if approval is None or approval["status"] not in ("approved", "dismissed"):
            return None
        if approval["status"] == "dismissed":
            return approval
        if approval["uses"] >= approval["max_uses"]:
            return None
        if approval["expires_at"] is not None and db.now() > approval["expires_at"]:
            return None
        return approval

    def consume_grant(self, approval_id: int) -> dict[str, Any]:
        """Spend one use of a grant. Called only when the gate actually opens, so the
        count reflects attempts that ran, not attempts that were merely considered.

        A dismissed row counts its uses too, though nothing enforces the limit for it:
        the number is how often one classifier bug actually cost a worker something.
        """
        self.conn.execute("UPDATE approvals SET uses = uses + 1 WHERE id=?", (approval_id,))
        approval = self.get_approval(approval_id)
        assert approval is not None
        self.add_event(approval["wo_id"], "gate_opened", {
            "approval_id": approval_id,
            "kind": approval["kind"],
            "use": approval["uses"],
            "of": approval["max_uses"],
            # Which of the two clearing statuses opened it. The timeline must not report
            # a dismissed false positive as "ran the approved command".
            "clearance": approval["status"],
        })
        return approval

    def list_approvals(self, wo_id: str | None = None,
                       statuses: tuple[str, ...] | None = None,
                       limit: int = 200) -> list[dict[str, Any]]:
        q = "SELECT * FROM approvals"
        conds: list[str] = []
        params: list[Any] = []
        if wo_id:
            conds.append("wo_id=?")
            params.append(wo_id)
        if statuses:
            conds.append(f"status IN ({','.join('?' for _ in statuses)})")
            params.extend(statuses)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY ts DESC, id DESC LIMIT ?"
        params.append(limit)
        return db.rows_to_dicts(self.conn.execute(q, params).fetchall())

    def pending_approvals(self, wo_id: str | None = None) -> list[dict[str, Any]]:
        """Requests still awaiting a verdict — from Neo or, once escalated, the user."""
        return self.list_approvals(wo_id, statuses=("pending",))

    def expire_approvals(self) -> int:
        """Move spent or timed-out grants to `expired` so listings tell the truth.

        Cosmetic for enforcement — `usable_grant` already refuses them — but a dashboard
        showing a month-old "approved" release gate reads as standing permission, which
        is exactly the wrong impression to leave lying around.

        `status='approved'` in the WHERE clause is doing two jobs, and the second one is
        load-bearing: it EXCLUDES `dismissed`. A dismissal never expires, and sweeping it
        into `expired` would also erase the false-positive count that
        `dismissed_count()` exists to report — the whole reason the verdict is separate.
        """
        cur = self.conn.execute(
            """UPDATE approvals SET status='expired'
               WHERE status='approved'
                 AND (uses >= max_uses OR (expires_at IS NOT NULL AND expires_at < ?))""",
            (db.now(),),
        )
        return cur.rowcount

    def dismissed_count(self, wo_id: str | None = None) -> int:
        """How many gate requests turned out not to be gated actions at all.

        The OS's classifier false-positive rate, in one number. It is the signal for
        whether the recognisers in `gates.KINDS` are getting better or worse, so it is
        counted from the rows rather than derived from anything a reviewer wrote.
        """
        q = "SELECT COUNT(*) c FROM approvals WHERE status='dismissed'"
        params: list[Any] = []
        if wo_id:
            q += " AND wo_id=?"
            params.append(wo_id)
        return int(self.conn.execute(q, params).fetchone()["c"])

    # -- summary ----------------------------------------------------------------

    def summary(self) -> dict[str, Any]:
        by_status = {
            r["status"]: r["c"]
            for r in self.conn.execute(
                "SELECT status, COUNT(*) c FROM work_orders WHERE hidden=0 GROUP BY status"
            ).fetchall()
        }
        attention = self.conn.execute(
            "SELECT COUNT(*) c FROM work_orders WHERE needs_attention=1 AND hidden=0"
        ).fetchone()["c"]
        pending_assumptions = len(self.pending_assumptions())
        return {
            "by_status": by_status,
            "needs_attention": attention,
            "pending_assumptions": pending_assumptions,
        }
