"""Neo's store: $JARVIS_HOME/neo.db

Neo is the OS-level answerer agent: it responds to worker questions on the user's
behalf. This DB is Neo's own (separate from os.db by design): the question queue,
every answer Neo gave, the user's reviews of those answers, and the learnings
distilled from them.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from . import db
from .paths import ensure_home, neo_db_path

# Question lifecycle.
Q_STATUSES = (
    "queued",     # waiting for Neo
    "answering",  # claimed by the Neo drain loop
    "answered",   # Neo answered; answer delivered to the worker
    "escalated",  # Neo declined — the user must answer
    "failed",     # answering errored; surfaced as attention
)
# Review lifecycle of an answered question.
REVIEW_STATUSES = ("unreviewed", "approved", "corrected")

SCHEMA = """
CREATE TABLE IF NOT EXISTS questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    project TEXT NOT NULL,
    wo_id TEXT NOT NULL,
    question TEXT NOT NULL,
    context TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'queued',
    answer TEXT,
    answered_by TEXT,                        -- neo | user
    answer_reason TEXT,                      -- Neo's stated reasoning / escalation reason
    answered_at REAL,
    review_status TEXT NOT NULL DEFAULT 'unreviewed',
    review_feedback TEXT,
    reviewed_at REAL
);
CREATE TABLE IF NOT EXISTS learnings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    project TEXT NOT NULL DEFAULT '',        -- '' = applies everywhere
    content TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual',   -- review | escalation | manual
    question_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_q_status ON questions(status);
CREATE INDEX IF NOT EXISTS idx_q_review ON questions(review_status);
CREATE INDEX IF NOT EXISTS idx_learn_project ON learnings(project);
"""


class NeoStore:
    def __init__(self, path: Path | None = None):
        ensure_home()
        self.db_path = path or neo_db_path()
        self.conn = db.connect(self.db_path)
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    # -- questions -------------------------------------------------------------

    def ask(self, project: str, wo_id: str, question: str, context: str = "") -> dict[str, Any]:
        cur = self.conn.execute(
            "INSERT INTO questions (ts, project, wo_id, question, context) VALUES (?,?,?,?,?)",
            (db.now(), project, wo_id, question, context),
        )
        return self.get(int(cur.lastrowid))  # type: ignore[return-value]

    def get(self, question_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM questions WHERE id=?", (question_id,)).fetchone()
        return dict(row) if row else None

    def claim_next(self) -> dict[str, Any] | None:
        """Atomically claim the OLDEST queued question (FIFO — answering in order
        keeps Neo's shared prompt prefix warm in the Anthropic cache)."""
        cur = self.conn.execute(
            """UPDATE questions SET status='answering'
               WHERE id = (SELECT id FROM questions WHERE status='queued'
                           ORDER BY ts LIMIT 1)
               RETURNING *""",
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def record_answer(self, question_id: int, answer: str, answered_by: str = "neo",
                      reason: str = "") -> None:
        self.conn.execute(
            """UPDATE questions SET status='answered', answer=?, answered_by=?,
               answer_reason=?, answered_at=? WHERE id=?""",
            (answer, answered_by, reason, db.now(), question_id),
        )

    def mark(self, question_id: int, status: str, reason: str = "") -> None:
        assert status in Q_STATUSES, status
        self.conn.execute(
            "UPDATE questions SET status=?, answer_reason=COALESCE(NULLIF(?,''), answer_reason) WHERE id=?",
            (status, reason, question_id),
        )

    def list_questions(self, statuses: tuple[str, ...] | None = None,
                       review_status: str | None = None, limit: int = 200) -> list[dict[str, Any]]:
        q = "SELECT * FROM questions"
        conds, params = [], []
        if statuses:
            conds.append(f"status IN ({','.join('?' for _ in statuses)})")
            params.extend(statuses)
        if review_status:
            conds.append("review_status=?")
            params.append(review_status)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        q += " ORDER BY ts DESC LIMIT ?"
        params.append(limit)
        return db.rows_to_dicts(self.conn.execute(q, params).fetchall())

    def counts(self) -> dict[str, int]:
        by_status = {
            r["status"]: r["c"]
            for r in self.conn.execute(
                "SELECT status, COUNT(*) c FROM questions GROUP BY status"
            ).fetchall()
        }
        unreviewed = self.conn.execute(
            "SELECT COUNT(*) c FROM questions WHERE status='answered' "
            "AND answered_by='neo' AND review_status='unreviewed'"
        ).fetchone()["c"]
        return {**by_status, "unreviewed": unreviewed}

    # -- review loop -----------------------------------------------------------

    def review(self, question_id: int, approved: bool, feedback: str = "") -> dict[str, Any]:
        q = self.get(question_id)
        if q is None:
            raise KeyError(f"neo question {question_id} not found")
        status = "approved" if approved else "corrected"
        self.conn.execute(
            "UPDATE questions SET review_status=?, review_feedback=?, reviewed_at=? WHERE id=?",
            (status, feedback, db.now(), question_id),
        )
        return self.get(question_id)  # type: ignore[return-value]

    # -- learnings ---------------------------------------------------------------

    def add_learning(self, content: str, project: str = "", source: str = "manual",
                     question_id: int | None = None) -> dict[str, Any]:
        cur = self.conn.execute(
            "INSERT INTO learnings (ts, project, content, source, question_id) VALUES (?,?,?,?,?)",
            (db.now(), project, content, source, question_id),
        )
        row = self.conn.execute("SELECT * FROM learnings WHERE id=?", (cur.lastrowid,)).fetchone()
        return dict(row)

    def learnings(self, project: str = "", limit: int = 50) -> list[dict[str, Any]]:
        """Learnings relevant to a project (its own + global), OLDEST first.

        Oldest-first keeps the rendered learnings block append-only: a new learning
        extends Neo's prompt prefix instead of rewriting it, so previously cached
        prefix bytes stay valid. When over the limit, the newest N are kept (rendered
        still in ascending order — a one-time prefix shift per overflow).
        """
        rows = self.conn.execute(
            """SELECT * FROM (
                   SELECT * FROM learnings WHERE project=? OR project=''
                   ORDER BY ts DESC LIMIT ?
               ) ORDER BY ts""",
            (project, limit),
        ).fetchall()
        return db.rows_to_dicts(rows)

    def all_learnings(self, limit: int = 200) -> list[dict[str, Any]]:
        """Every learning regardless of project scope (review surfaces)."""
        rows = self.conn.execute(
            "SELECT * FROM learnings ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
        return db.rows_to_dicts(rows)
