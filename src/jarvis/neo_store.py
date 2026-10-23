"""Neo's store: $JARVIS_HOME/neo.db"""

from __future__ import annotations

from collections.abc import Sequence
from pathlib import Path
from typing import Any

from . import db
from .paths import ensure_home, neo_db_path

# Question lifecycle
Q_STATUSES = (
    "queued",     # waiting for Neo
    "answering",  # claimed by the Neo drain loop
    "answered",   # Neo answered; answer delivered to the worker
    "escalated",  # Neo declined — the user must answer
    "failed",     # answering errored; surfaced as attention
)
# Review lifecycle of an answered question
REVIEW_STATUSES = ("unreviewed", "approved", "corrected")

# The two halves of "still open", and WHO is holding it
NEO_HELD_Q_STATUSES = ("queued", "answering")
USER_HELD_Q_STATUSES = ("escalated", "failed")
OPEN_Q_STATUSES = NEO_HELD_Q_STATUSES + USER_HELD_Q_STATUSES

# What Neo is being asked for (docs/superpowers/specs/2026-08-31-the-supervisor.md)
#: `validation` is Morpheus asking about a shipped PR
Q_KINDS = ("question", "approval", "plan", "alarm", "triage", "assumption", "validation",
           "findings")

# The panel's seats — see docs/superpowers/specs/2026-08-02-neo-team-design.md
SEATS = ("premise", "record", "blast", "taste", "chair")

#: The alarm supervisor's learnings live in this table too, scoped by the same `seat` column (docs/superpowers/specs/2026-08-31-the-supervisor.md)
SUPERVISOR_SEAT = "supervisor"

# The seat values a LEARNING may legally carry, which is a wider vocabulary than the panel's roster
LEARNING_SCOPES = SEATS + (SUPERVISOR_SEAT,)

# WHY an escalation happened, as one groupable label (docs/superpowers/specs/2026-10-01-neo-observability.md)
ESCALATION_CAUSES_CHOSEN = (
    "high-stakes",
    "no-learning-applies",
    "conflicting-authority",
    "ambiguous-intent",
    "scope-too-large",
    "privileged-action",
    "evidence-insufficient",
    "user-decision",
)
# OVERRIDDEN is the third answer and the largest escalation population: NEO ANSWERED AND THE OS DID...
ESCALATION_CAUSES_OVERRIDDEN = (
    "stakes-high",            # Neo accepted and flagged it high; the OS obeyed
    "stakes-unclassified",    # no readable stakes, and silence is not routine
    "stakes-unreadable",      # a stakes word the OS cannot read as routine
    "neo-denied",             # `verdict: deny`, and there is no machine rejection
    "scope-over-cap",         # children at or over `plans.CHILD_CAP`
)
# FAILED is derived by the OS at the code path too
ESCALATION_CAUSES_FAILED = (
    "transport-unreachable",
    "attempts-exhausted",
    "prompt-refused",
    "unparseable-reply",
)
ESCALATION_CAUSES = (ESCALATION_CAUSES_CHOSEN + ESCALATION_CAUSES_OVERRIDDEN
                     + ESCALATION_CAUSES_FAILED)

# How one seat's contribution ended
OPINION_STATUSES = ("ok", "abstained", "failed")

# A question claimed for answering longer than this is treated as stranded and returned to the queue...
STALE_ANSWERING_SECONDS = 900

# How many times a question may be returned to the queue before it is given up on
MAX_ANSWER_ATTEMPTS = 3

#: `answer_reason` on a question nobody could put to a model (docs/superpowers/specs/2026-09-18-a-failure-is-not-an-answer.md §2.)
UNREACHABLE_PREFIX = "neo could not be reached: "


class QuestionTooLargeError(ValueError):
    """`ask` refused to persist a question past the ceiling (docs/superpowers/specs/2026-09-26-bounded-model-inputs.md)"""

    def __init__(self, message: str, *, question_chars: int, context_chars: int,
                 ceiling: int) -> None:
        super().__init__(message)
        self.question_chars = question_chars
        self.context_chars = context_chars
        self.ceiling = ceiling


#: How long a question held back by a transport failure waits before it may be claimed again (#235)
RETRY_BACKOFF_SECONDS = (60.0, 300.0, 900.0)


def retry_delay(attempts: int) -> float:
    """Seconds to hold a question that has already spent `attempts` attempts"""
    return RETRY_BACKOFF_SECONDS[min(max(attempts, 0), len(RETRY_BACKOFF_SECONDS) - 1)]


SCHEMA = """
CREATE TABLE IF NOT EXISTS questions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    project TEXT NOT NULL,
    wo_id TEXT NOT NULL,
    question TEXT NOT NULL,
    context TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'queued',
    kind TEXT NOT NULL DEFAULT 'question',   -- question | approval
    answer TEXT,
    answered_by TEXT,                        -- neo | user
    answer_reason TEXT,                      -- Neo's stated reasoning / escalation reason
    -- ESCALATION_CAUSES, or NULL for "not recorded". Also in ADDED_COLUMNS, where the
    -- reasoning for the nullability is.
    escalation_cause TEXT,
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
    question_id INTEGER,
    seat TEXT NOT NULL DEFAULT '',           -- '' = global (every seat sees it)
    retired_at REAL,                         -- NULL = standing; set = superseded
    retired_reason TEXT                      -- why, in the user's words
);
CREATE TABLE IF NOT EXISTS panel_opinions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    question_id INTEGER NOT NULL REFERENCES questions(id) ON DELETE CASCADE,
    seat TEXT NOT NULL,
    reply TEXT NOT NULL DEFAULT '',          -- the seat's raw reply, verbatim
    verdict TEXT NOT NULL DEFAULT '',        -- the verdict it proposed, if any
    route TEXT NOT NULL DEFAULT '',          -- fast | panel — only `premise` routes
    status TEXT NOT NULL DEFAULT 'ok',       -- ok | abstained | failed
    model TEXT NOT NULL DEFAULT '',
    latency_ms INTEGER NOT NULL DEFAULT 0,
    UNIQUE (question_id, seat)
);
CREATE INDEX IF NOT EXISTS idx_q_status ON questions(status);
CREATE INDEX IF NOT EXISTS idx_q_review ON questions(review_status);
CREATE INDEX IF NOT EXISTS idx_learn_project ON learnings(project);
"""

# Columns added after the first release
ADDED_COLUMNS = {
    "questions": {
        "kind": "TEXT NOT NULL DEFAULT 'question'",
        # Stranded-question recovery
        "attempts": "INTEGER NOT NULL DEFAULT 0",
        "claimed_at": "REAL",
        # Transport backoff (`release_claim`)
        "retry_after": "REAL",
        # The dashboard's shortened rendering of an over-long question, as JSON — see `jarvis.digest`
        "digest": "TEXT",
        # Why this escalation happened — §1 of docs/superpowers/specs/2026-10-01-neo-observability.md
        "escalation_cause": "TEXT",
    },
    "learnings": {
        "seat": "TEXT NOT NULL DEFAULT ''",
        # Retraction
        "retired_at": "REAL",
        "retired_reason": "TEXT",
    },
}


class NeoStore:
    def __init__(self, path: Path | None = None):
        ensure_home()
        self.db_path = path or neo_db_path()
        self.conn = db.connect(self.db_path)
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        db.add_missing_columns(self.conn, ADDED_COLUMNS)

    def close(self) -> None:
        self.conn.close()

    # -- questions

    def ask(self, project: str, wo_id: str, question: str, context: str = "",
            kind: str = "question") -> dict[str, Any]:
        assert kind in Q_KINDS, kind
        # REFUSE, never trim, and never persist: a question is stored before it is ever sent (docs/superpowers/specs/2026-09-26-bounded-model-inputs.md)
        from . import claude_cli

        size = len(question) + len(context)
        ceiling = claude_cli.MAX_OS_PROMPT_CHARS
        if size > ceiling:
            # Numbers only — no fragment of the question: this reaches the inbox
            raise QuestionTooLargeError(
                f"refused to store a {kind}: question + context is {size} chars, over "
                f"the {ceiling}-char ceiling (os.max_os_prompt_chars) — shorten it and "
                f"ask again, referencing what you would have pasted",
                question_chars=len(question), context_chars=len(context),
                ceiling=ceiling)
        cur = self.conn.execute(
            "INSERT INTO questions (ts, project, wo_id, question, context, kind) "
            "VALUES (?,?,?,?,?,?)",
            (db.now(), project, wo_id, question, context, kind),
        )
        return self.get(int(cur.lastrowid))  # type: ignore[return-value]

    def get(self, question_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM questions WHERE id=?", (question_id,)).fetchone()
        return dict(row) if row else None

    def revise_question(self, question_id: int, question: str) -> bool:
        """Rewrite an OPEN question's text where it stands"""
        q = self.get(question_id)
        if q is None or q["status"] not in OPEN_Q_STATUSES:
            return False
        # The digest is a rendering of the old text — see `set_digest`
        self.conn.execute("UPDATE questions SET question=?, digest=NULL WHERE id=?",
                          (question, question_id))
        return True

    def claim_next(self) -> dict[str, Any] | None:
        """Atomically claim the OLDEST queued question (FIFO"""
        now = db.now()
        cur = self.conn.execute(
            """UPDATE questions SET status='answering', claimed_at=?
               WHERE id = (SELECT id FROM questions WHERE status='queued'
                             AND COALESCE(retry_after, 0) <= ?
                           ORDER BY ts LIMIT 1)
               RETURNING *""",
            (now, now),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def reclaim_stale(self, older_than: float = STALE_ANSWERING_SECONDS,
                      max_attempts: int = MAX_ANSWER_ATTEMPTS) -> dict[str, list[int]]:
        """Unstick questions parked in `answering` by a drain that never finished"""
        cutoff = db.now() - older_than
        # Give up FIRST, then re-queue: doing it the other way round would increment a row to the ceiling and...
        failed = [
            int(r["id"])
            for r in self.conn.execute(
                """UPDATE questions
                      SET status='failed',
                          escalation_cause='attempts-exhausted',
                          answer_reason='neo could not be reached: stranded in '
                                        || 'answering after ' || attempts
                                        || ' reclaim attempt(s) — nobody has judged this'
                    WHERE status='answering' AND attempts >= ?
                      AND COALESCE(claimed_at, ts) < ?
                RETURNING id""",
                (max_attempts, cutoff),
            ).fetchall()
        ]
        # NO `retry_after` here, unlike `release_claim` (#235)
        requeued = [
            int(r["id"])
            for r in self.conn.execute(
                """UPDATE questions
                      SET status='queued', attempts=attempts+1, claimed_at=NULL,
                          retry_after=NULL
                    WHERE status='answering' AND attempts < ?
                      AND COALESCE(claimed_at, ts) < ?
                RETURNING id""",
                (max_attempts, cutoff),
            ).fetchall()
        ]
        return {"requeued": requeued, "failed": failed}

    def clear_retry_hold(self, question_id: int) -> None:
        """Make a held question claimable NOW — the user asking again is not a retry"""
        self.conn.execute("UPDATE questions SET retry_after=NULL WHERE id=?",
                          (question_id,))

    def hold_claim(self, question_id: int, detail: str, reopens_at: float) -> str:
        """Park a claimed question until a stated moment, SPENDING NO ATTEMPT (#235)"""
        self.conn.execute(
            "UPDATE questions SET status='queued', claimed_at=NULL, retry_after=?, "
            "answer_reason=? WHERE id=?",
            (reopens_at, f"{UNREACHABLE_PREFIX}{detail}", question_id))
        return "queued"

    def release_claim(self, question_id: int, detail: str,
                      max_attempts: int = MAX_ANSWER_ATTEMPTS) -> str:
        """Hand a claimed question back after a call that produced NO ANSWER AT ALL (docs/superpowers/specs/2026-09-18-a-failure-is-not-an-answer.md §2.)"""
        q = self.get(question_id)
        if q is None:
            return "unreachable"
        attempts = int(q["attempts"] or 0)
        reason = f"{UNREACHABLE_PREFIX}{detail}"
        if attempts >= max_attempts:
            # `max_attempts=0` is the REFUSAL contract: the only callers that pass it are the prompt/input... (docs/superpowers/specs/2026-10-01-neo-observability.md)
            cause = "prompt-refused" if max_attempts == 0 else "transport-unreachable"
            self.conn.execute(
                "UPDATE questions SET status='failed', claimed_at=NULL, answer_reason=?, "
                "escalation_cause=? WHERE id=?",
                (f"{reason} (after {attempts} retries — nobody has judged this)",
                 cause, question_id))
            return "unreachable"
        self.conn.execute(
            "UPDATE questions SET status='queued', attempts=attempts+1, claimed_at=NULL, "
            "retry_after=?, answer_reason=? WHERE id=?",
            (db.now() + retry_delay(attempts), reason, question_id))
        return "queued"

    def record_answer(self, question_id: int, answer: str, answered_by: str = "neo",
                      reason: str = "") -> None:
        self.conn.execute(
            """UPDATE questions SET status='answered', answer=?, answered_by=?,
               answer_reason=?, answered_at=? WHERE id=?""",
            (answer, answered_by, reason, db.now(), question_id),
        )

    def supersede(self, question_id: int, answer: str, reason: str) -> bool:
        """Close an open question whose decision was taken somewhere else"""
        q = self.get(question_id)
        if q is None or q["status"] not in OPEN_Q_STATUSES:
            return False
        self.record_answer(question_id, answer, answered_by="os", reason=reason)
        return True

    def mark(self, question_id: int, status: str, reason: str = "",
             cause: str = "") -> None:
        # `cause` is ESCALATION_CAUSES or "" for "not recorded" (docs/superpowers/specs/2026-10-01-neo-observability.md)
        assert status in Q_STATUSES, status
        assert cause == "" or cause in ESCALATION_CAUSES, cause
        self.conn.execute(
            "UPDATE questions SET status=?, "
            "answer_reason=COALESCE(NULLIF(?,''), answer_reason), "
            "escalation_cause=COALESCE(NULLIF(?,''), escalation_cause) WHERE id=?",
            (status, reason, cause, question_id),
        )

    # -- digests (the dashboard's shortened rendering — see `jarvis.digest`)

    def set_digest(self, question_id: int, digest: str) -> None:
        """Record the digest, or the recorded failure to produce one"""
        self.conn.execute("UPDATE questions SET digest=? WHERE id=?",
                          (digest, question_id))

    def questions_needing_digest(self, min_chars: int,
                                 limit: int = 20) -> list[dict[str, Any]]:
        """Long questions the user will actually be shown, that nobody has digested yet"""
        return db.rows_to_dicts(self.conn.execute(
            """SELECT * FROM questions
                WHERE digest IS NULL
                  AND LENGTH(question) >= ?
                  AND (status IN ('escalated', 'failed')
                       OR (status = 'answered' AND answered_by = 'neo'
                           AND review_status = 'unreviewed'))
             ORDER BY ts DESC LIMIT ?""",
            (min_chars, limit),
        ).fetchall())

    def purge_work_order(self, wo_id: str) -> int:
        """Forget the questions asked by a deleted work order"""
        self.conn.execute(
            """UPDATE learnings SET question_id=NULL WHERE question_id IN
               (SELECT id FROM questions WHERE wo_id=?)""",
            (wo_id,),
        )
        return self.conn.execute("DELETE FROM questions WHERE wo_id=?", (wo_id,)).rowcount

    def open_questions(self, wo_id: str) -> list[dict[str, Any]]:
        """Questions this work order is still parked on, oldest first"""
        return db.rows_to_dicts(self.conn.execute(
            f"""SELECT * FROM questions
                WHERE wo_id=? AND kind='question'
                  AND status IN ({','.join('?' for _ in OPEN_Q_STATUSES)})
                ORDER BY ts""",
            (wo_id, *OPEN_Q_STATUSES),
        ).fetchall())

    def answered_questions(self, wo_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """Decisions already taken on this work order, NEWEST FIRST (#832)"""
        return db.rows_to_dicts(self.conn.execute(
            """SELECT * FROM questions WHERE wo_id=? AND status='answered'
               ORDER BY ts DESC, id DESC LIMIT ?""",
            (wo_id, limit),
        ).fetchall())

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

    def search_questions(self, words: Sequence[str], project: str = "",
                         limit: int = 50) -> list[dict[str, Any]]:
        """Questions matching `words`, answered ones included: a ruling the user wants to find again is by..."""
        expr, params = db.score_sql(words, {
            "question": 3, "wo_id": 2, "answer": 1, "answer_reason": 1, "context": 1,
            "review_feedback": 1,
        })
        q = [f"SELECT *, {expr} AS _score FROM questions WHERE _score > 0"]
        if project:
            q.append("AND project=?")
            params.append(project)
        q.append("ORDER BY _score DESC, ts DESC LIMIT ?")
        rows = self.conn.execute(" ".join(q), (*params, limit)).fetchall()
        return db.rows_to_dicts(rows)

    def question_outcomes(self, project: str = "",
                          since: float | None = None) -> list[dict[str, Any]]:
        """Every question in the window, grouped on (kind, project, day, outcome) (docs/superpowers/specs/2026-10-01-neo-observability.md)"""
        where, params = [], []
        if project:
            where.append("project=?")
            params.append(project)
        if since is not None:
            where.append("ts >= ?")
            params.append(since)
        clause = f"WHERE {' AND '.join(where)}" if where else ""
        return db.rows_to_dicts(self.conn.execute(
            f"""SELECT kind, project, status, COALESCE(answered_by, '') AS answered_by,
                       strftime('%Y-%m-%d', ts, 'unixepoch', 'localtime') AS day,
                       COUNT(*) AS n
                FROM questions {clause}
                GROUP BY kind, project, status, answered_by, day
                ORDER BY day""", params).fetchall())

    def escalation_cause_counts(self, project: str = "",
                                since: float | None = None) -> list[dict[str, Any]]:
        """`GROUP BY escalation_cause` over the handed-back questions"""
        where = ["status IN ('escalated','failed')"]
        params: list[Any] = []
        if project:
            where.append("project=?")
            params.append(project)
        if since is not None:
            where.append("ts >= ?")
            params.append(since)
        return db.rows_to_dicts(self.conn.execute(
            f"""SELECT escalation_cause AS cause, COUNT(*) AS n
                FROM questions WHERE {' AND '.join(where)}
                GROUP BY escalation_cause""", params).fetchall())

    def questions_per_order(self, project: str = "",
                            since: float | None = None) -> list[dict[str, Any]]:
        """Questions in the window by the order that asked, TRIAGE EXCLUDED"""
        where = ["kind != 'triage'", "wo_id != ''"]
        params: list[Any] = []
        if project:
            where.append("project=?")
            params.append(project)
        if since is not None:
            where.append("ts >= ?")
            params.append(since)
        return db.rows_to_dicts(self.conn.execute(
            f"""SELECT project, wo_id, COUNT(*) AS n
                FROM questions WHERE {' AND '.join(where)}
                GROUP BY project, wo_id""", params).fetchall())

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

    # -- review loop

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

    # -- panel deliberation

    def record_opinion(self, question_id: int, seat: str, reply: str = "",
                       verdict: str = "", route: str = "", status: str = "ok",
                       model: str = "", latency_ms: int = 0) -> dict[str, Any]:
        """Store one seat's contribution to one decision"""
        assert status in OPINION_STATUSES, status
        self.conn.execute(
            """INSERT INTO panel_opinions
                   (ts, question_id, seat, reply, verdict, route, status, model,
                    latency_ms)
               VALUES (?,?,?,?,?,?,?,?,?)
               ON CONFLICT(question_id, seat) DO UPDATE SET
                   ts=excluded.ts, reply=excluded.reply, verdict=excluded.verdict,
                   route=excluded.route, status=excluded.status, model=excluded.model,
                   latency_ms=excluded.latency_ms""",
            (db.now(), question_id, seat, reply, verdict, route, status, model,
             latency_ms),
        )
        row = self.conn.execute(
            "SELECT * FROM panel_opinions WHERE question_id=? AND seat=?",
            (question_id, seat),
        ).fetchone()
        return dict(row)

    def opinions(self, question_id: int) -> list[dict[str, Any]]:
        """Every seat's opinion on one question, in the order the seats first reported"""
        return db.rows_to_dicts(self.conn.execute(
            "SELECT * FROM panel_opinions WHERE question_id=? ORDER BY id",
            (question_id,),
        ).fetchall())

    # -- learnings

    def add_learning(self, content: str, project: str = "", source: str = "manual",
                     question_id: int | None = None, seat: str = "") -> dict[str, Any]:
        """Record a learning"""
        cur = self.conn.execute(
            "INSERT INTO learnings (ts, project, content, source, question_id, seat) "
            "VALUES (?,?,?,?,?,?)",
            (db.now(), project, content, source, question_id, seat or ""),
        )
        row = self.conn.execute("SELECT * FROM learnings WHERE id=?", (cur.lastrowid,)).fetchone()
        return dict(row)

    def has_active_learning(self, content: str, project: str = "") -> bool:
        """Is this exact sentence already an unretired learning of this project?"""
        return self.conn.execute(
            "SELECT 1 FROM learnings WHERE project=? AND content=? AND retired_at IS NULL",
            (project, content)).fetchone() is not None

    def retract_learning(self, learning_id: int, reason: str) -> dict[str, Any]:
        """Retire a learning the user has superseded"""
        if not reason.strip():
            raise ValueError("a retraction needs a reason: what supersedes this learning?")
        row = self.conn.execute(
            "SELECT * FROM learnings WHERE id=?", (learning_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"learning {learning_id} not found")
        if row["retired_at"] is not None:
            raise ValueError(
                f"learning {learning_id} was already retired: {row['retired_reason']!r}")
        self.conn.execute(
            "UPDATE learnings SET retired_at=?, retired_reason=? WHERE id=?",
            (db.now(), reason.strip(), learning_id),
        )
        return dict(self.conn.execute(
            "SELECT * FROM learnings WHERE id=?", (learning_id,)).fetchone())

    def learnings(self, project: str = "", limit: int = 50, seat: str = "",
                  include_retired: bool = False) -> list[dict[str, Any]]:
        """Learnings relevant to a project (its own + global), OLDEST first"""
        retired = "" if include_retired else " AND retired_at IS NULL"
        rows = self.conn.execute(
            f"""SELECT * FROM (
                   SELECT * FROM learnings
                    WHERE (project=? OR project='') AND (seat='' OR seat=?){retired}
                   ORDER BY ts DESC LIMIT ?
               ) ORDER BY ts""",
            (project, seat or "", limit),
        ).fetchall()
        return db.rows_to_dicts(rows)

    def all_learnings(self, limit: int = 200) -> list[dict[str, Any]]:
        """Every learning regardless of project scope (review surfaces)"""
        rows = self.conn.execute(
            "SELECT * FROM learnings ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
        return db.rows_to_dicts(rows)

    # -- export

    #: The tables `export` dumps, in the order they appear in the document
    EXPORT_TABLES = ("questions", "learnings", "panel_opinions")

    def export(self) -> dict[str, list[dict[str, Any]]]:
        """The whole ledger: every table, every row, every column, no truncation"""
        return {
            table: db.rows_to_dicts(
                self.conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall())
            for table in self.EXPORT_TABLES
        }
