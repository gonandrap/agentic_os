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

# What Neo is being asked for. `question` is an open decision ("which library?");
# `approval` is a privileged-action gate ("may I merge this PR?"); `plan` is a feature
# order's decomposition ("release these six work orders?"). Each of the latter two gets
# its own persona and a verdict with an approve/reject bit — see gates.py and plans.py.
#
# `plan` is deliberately NOT routed through the approvals table even though the shape
# rhymes: `approvals` is a receipt for one command string, and its `dismissed` count is
# the OS's classifier false-positive rate. Plan reviews are neither, and mixing them in
# would corrupt the one metric that says whether the gate recognisers are improving.
Q_KINDS = ("question", "approval", "plan")

# The panel's seats — see docs/superpowers/specs/2026-08-02-neo-team-design.md.
# `premise` asks whether this was even the question that was asked (and routes),
# `record` checks the standing rulings, `blast` owns the blast radius and the only veto,
# `taste` speaks for the user's intent and attention. `chair` is not a fifth opinion: it
# synthesises the others into the one strict-JSON verdict `neo.parse_verdict` accepts.
#
# The vocabulary lives HERE, in the lowest layer, rather than in the panel module: the
# catalog roster validator and the CLI's `--seat` validator both need it and neither
# should have to depend on the other.
SEATS = ("premise", "record", "blast", "taste", "chair")

# How one seat's contribution ended. A seat that errors or times out is recorded as
# `abstained` and the panel proceeds; `failed` is for a call that came back unusable.
OPINION_STATUSES = ("ok", "abstained", "failed")

# A question claimed for answering longer than this is treated as stranded and returned
# to the queue (see `NeoStore.reclaim_stale`).
#
# THIS MUST EXCEED THE LONGEST A LIVE NEO CALL CAN TAKE (`catalog.NeoConfig.timeout`,
# 300s — pinned by a test). A cutoff below it re-queues a question out from under a call
# that is still running, and the worker gets two answers to one question.
STALE_ANSWERING_SECONDS = 900

# How many times a question may be returned to the queue before it is given up on. The
# initial claim is not an attempt: `attempts` counts the reclaims, so this is the number
# of RETRIES. A question at the ceiling is marked `failed` rather than re-queued —
# `queued` would loop for ever and `answering` would strand it again, and `failed` is the
# only status the existing surfaces already render as attention.
MAX_ANSWER_ATTEMPTS = 3

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

# Columns added after the first release. `CREATE TABLE IF NOT EXISTS` is a no-op on an
# existing database, so new columns must be ALTERed in on open — every live instance
# already has a neo.db from before these existed.
ADDED_COLUMNS = {
    "questions": {
        "kind": "TEXT NOT NULL DEFAULT 'question'",
        # Stranded-question recovery. `claimed_at` is NULL on every row that predates
        # this, which `reclaim_stale` reads as "as stale as it is old" — the questions
        # already parked in `answering` on a live instance are exactly the ones the fix
        # exists for, so they must not be exempt from it.
        "attempts": "INTEGER NOT NULL DEFAULT 0",
        "claimed_at": "REAL",
        # The dashboard's shortened rendering of an over-long question, as JSON — see
        # `jarvis.digest`. NULL means "never attempted", which is what every row that
        # predates this feature is, and what the daemon looks for. It is a DISPLAY
        # artefact: nothing that reaches Neo, a worker or a learning is built from it.
        "digest": "TEXT",
    },
    "learnings": {
        "seat": "TEXT NOT NULL DEFAULT ''",
        # Retraction. NULL on every pre-existing row, which reads as "standing" — the
        # ledger was append-only until now, so nothing that predates this is retired.
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

    # -- questions -------------------------------------------------------------

    def ask(self, project: str, wo_id: str, question: str, context: str = "",
            kind: str = "question") -> dict[str, Any]:
        assert kind in Q_KINDS, kind
        cur = self.conn.execute(
            "INSERT INTO questions (ts, project, wo_id, question, context, kind) "
            "VALUES (?,?,?,?,?,?)",
            (db.now(), project, wo_id, question, context, kind),
        )
        return self.get(int(cur.lastrowid))  # type: ignore[return-value]

    def get(self, question_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM questions WHERE id=?", (question_id,)).fetchone()
        return dict(row) if row else None

    def claim_next(self) -> dict[str, Any] | None:
        """Atomically claim the OLDEST queued question (FIFO — answering in order
        keeps Neo's shared prompt prefix warm in the Anthropic cache)."""
        cur = self.conn.execute(
            """UPDATE questions SET status='answering', claimed_at=?
               WHERE id = (SELECT id FROM questions WHERE status='queued'
                           ORDER BY ts LIMIT 1)
               RETURNING *""",
            (db.now(),),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def reclaim_stale(self, older_than: float = STALE_ANSWERING_SECONDS,
                      max_attempts: int = MAX_ANSWER_ATTEMPTS) -> dict[str, list[int]]:
        """Unstick questions parked in `answering` by a drain that never finished.

        `claim_next` sets `answering` and, until this existed, NOTHING ever set it back:
        `neo.drain_queue` rescues only `ClaudeCliError` and the daemon wraps the whole
        drain in a bare `except`. A daemon restart mid-drain — or any other exception —
        therefore parked the question for ever, and the worker that asked it waited for
        ever (bl-3f5f1464).

        Rows past `older_than` seconds go back to `queued` with `attempts` incremented.
        A row already at `max_attempts` is marked `failed` instead: `queued` would loop
        for ever, `answering` would re-strand it, and `failed` is the status the OS
        already surfaces as attention.

        `older_than` MUST exceed the Neo call timeout — see STALE_ANSWERING_SECONDS.

        Returns {"requeued": [id, ...], "failed": [id, ...]}.
        """
        cutoff = db.now() - older_than
        # Give up FIRST, then re-queue: doing it the other way round would increment a
        # row to the ceiling and then fail it in the same call, spending an attempt the
        # question never got to use.
        failed = [
            int(r["id"])
            for r in self.conn.execute(
                """UPDATE questions
                      SET status='failed',
                          answer_reason='neo never answered: stranded in answering after '
                                        || attempts || ' reclaim attempt(s)'
                    WHERE status='answering' AND attempts >= ?
                      AND COALESCE(claimed_at, ts) < ?
                RETURNING id""",
                (max_attempts, cutoff),
            ).fetchall()
        ]
        requeued = [
            int(r["id"])
            for r in self.conn.execute(
                """UPDATE questions
                      SET status='queued', attempts=attempts+1, claimed_at=NULL
                    WHERE status='answering' AND attempts < ?
                      AND COALESCE(claimed_at, ts) < ?
                RETURNING id""",
                (max_attempts, cutoff),
            ).fetchall()
        ]
        return {"requeued": requeued, "failed": failed}

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

    # -- digests (the dashboard's shortened rendering — see `jarvis.digest`) ----

    def set_digest(self, question_id: int, digest: str) -> None:
        """Record the digest, or the recorded failure to produce one.

        Either way the column stops being NULL, which is what takes the row out of
        `questions_needing_digest`. Writing the failure is deliberate: see
        `digest.encode_failure`.
        """
        self.conn.execute("UPDATE questions SET digest=? WHERE id=?",
                          (digest, question_id))

    def questions_needing_digest(self, min_chars: int,
                                 limit: int = 20) -> list[dict[str, Any]]:
        """Long questions the user will actually be shown, that nobody has digested yet.

        THE STATUS FILTER IS THE COST CONTROL. A digest is one extra model call, so only
        the questions that reach a human's eyes on the `/neo` page earn one: the ones
        escalated to them, the ones that failed, and the answers awaiting their review.
        A question still `queued` or `answering` is deliberately excluded — it renders as
        a 160-character line in the in-flight strip and is about to change status anyway.

        Newest first: the questions the user is looking at now are the ones worth
        spending the call on if the batch is capped.
        """
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
        """Forget the questions asked by a deleted work order.

        Learnings survive: what Neo learned is durable knowledge about the user, not
        about the work order it happened to come from — only the back-link is cut.
        """
        self.conn.execute(
            """UPDATE learnings SET question_id=NULL WHERE question_id IN
               (SELECT id FROM questions WHERE wo_id=?)""",
            (wo_id,),
        )
        return self.conn.execute("DELETE FROM questions WHERE wo_id=?", (wo_id,)).rowcount

    def question_texts(self, wo_id: str) -> dict[int, str]:
        """{question_id: question} for one work order — the timeline's back-fill.

        Questions asked before the text was copied into the `question_asked` event
        payload exist only here, so a timeline rendered from the project DB alone
        cannot show them. This is read-only and best-effort: see
        `ops.neo_question_texts`.
        """
        return {
            int(r["id"]): r["question"] or ""
            for r in self.conn.execute(
                "SELECT id, question FROM questions WHERE wo_id=?", (wo_id,)
            ).fetchall()
        }

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

    # -- panel deliberation --------------------------------------------------------

    def record_opinion(self, question_id: int, seat: str, reply: str = "",
                       verdict: str = "", route: str = "", status: str = "ok",
                       model: str = "", latency_ms: int = 0) -> dict[str, Any]:
        """Store one seat's contribution to one decision.

        `route` is set by the `premise` seat alone (`fast` or `panel`); every other seat
        leaves it empty. A seat that errored or timed out is recorded with
        `status='abstained'` and the panel proceeds — that row is the evidence it ran.

        ONE ROW PER SEAT PER QUESTION, LAST RUN WINS. A question can genuinely be
        answered twice: `reclaim_stale` returns a stranded one to the queue, so a second
        panel run re-records the same seats. The upsert replaces the stranded attempt,
        because the deliberation a human wants to read is the one behind the answer that
        actually reached the worker (ruled by Neo, question 55). The row `id` survives
        the overwrite, so a surface holding a handle to an opinion keeps it.
        """
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
        """Every seat's opinion on one question, in the order the seats first reported.

        Ordered by `id` rather than `ts` so a partial re-run cannot reshuffle the rows a
        reader has already seen. Panel deliberation is inspectable on demand and is
        never delivered to the worker or placed in the inbox.
        """
        return db.rows_to_dicts(self.conn.execute(
            "SELECT * FROM panel_opinions WHERE question_id=? ORDER BY id",
            (question_id,),
        ).fetchall())

    # -- learnings ---------------------------------------------------------------

    def add_learning(self, content: str, project: str = "", source: str = "manual",
                     question_id: int | None = None, seat: str = "") -> dict[str, Any]:
        """Record a learning. `seat=""` (the default) means every seat sees it.

        A seat-scoped learning teaches the seat that got a decision wrong instead of all
        of them — and, critically, it stays OUT of the default query. See `learnings`.
        """
        cur = self.conn.execute(
            "INSERT INTO learnings (ts, project, content, source, question_id, seat) "
            "VALUES (?,?,?,?,?,?)",
            (db.now(), project, content, source, question_id, seat or ""),
        )
        row = self.conn.execute("SELECT * FROM learnings WHERE id=?", (cur.lastrowid,)).fetchone()
        return dict(row)

    def retract_learning(self, learning_id: int, reason: str) -> dict[str, Any]:
        """Retire a learning the user has superseded. NOT a delete.

        The row stays in the table and keeps being returned by `all_learnings` — that
        is the audit trail, and it is the whole reason this is an UPDATE. What changes
        is that `learnings` stops returning it, so it leaves Neo's system prompt.

        Retracting an already-retired learning RAISES rather than quietly re-stamping
        it: the original reason and timestamp are the record of when the user changed
        their mind, and a second retraction would overwrite both with no gain.
        """
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
        """Learnings relevant to a project (its own + global), OLDEST first.

        Oldest-first keeps the rendered learnings block append-only: a new learning
        extends Neo's prompt prefix instead of rewriting it, so previously cached
        prefix bytes stay valid. When over the limit, the newest N are kept (rendered
        still in ascending order — a one-time prefix shift per overflow).

        THE SEAT SCOPE IS ADDITIVE, AND THE DEFAULT IS NOT "EVERYTHING". No `seat`
        argument — and `seat=""` — return ONLY global rows; `seat="blast"` returns the
        global rows PLUS that seat's. The no-argument case is load-bearing:
        `neo.build_system_prompt` calls it that way for the single-agent path, so a
        seat-scoped learning leaking into it would silently rewrite a prompt prefix that
        has to stay byte-stable, and the panel ships disabled.

        RETIRED LEARNINGS ARE EXCLUDED BY DEFAULT, and the default is what matters: this
        is the method every prompt builder reads, including the panel's per-seat one,
        which lives in another module. Filtering here rather than in
        `neo.build_system_prompt` is what makes a seat-scoped prompt correct without its
        author having to know retraction exists (ruled by Neo, question 56). Audit
        surfaces — `all_learnings`, and `jarvis neo learnings` — pass
        `include_retired=True` and say so on the row.
        """
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
        """Every learning regardless of project scope (review surfaces).

        This is the AUDIT surface, so it returns retired learnings too, carrying their
        `retired_at` and `retired_reason`. A retraction retires a ruling; it does not
        erase that the user once held it.
        """
        rows = self.conn.execute(
            "SELECT * FROM learnings ORDER BY ts DESC LIMIT ?", (limit,)
        ).fetchall()
        return db.rows_to_dicts(rows)

    # -- export ------------------------------------------------------------------

    #: The tables `export` dumps, in the order they appear in the document.
    EXPORT_TABLES = ("questions", "learnings", "panel_opinions")

    def export(self) -> dict[str, list[dict[str, Any]]]:
        """The whole ledger: every table, every row, every column, no truncation.

        Prime directive 1 forbids reading `neo.db` directly, so this is the only way
        anything outside the OS gets at Neo's record — the panel eval replaying the
        question corpus is the first caller (`jarvis neo export --json`).

        `SELECT *` rather than a hand-written column list is deliberate. A column added
        to any of these tables ships in the export the day it lands, instead of being
        silently dropped until someone notices the field they wanted was never there.

        The document carries NO wall-clock field and every table is ordered by `id`, so
        two consecutive exports of an unchanged database are byte-identical and a diff
        between two of them shows only what actually changed.
        """
        return {
            table: db.rows_to_dicts(
                self.conn.execute(f"SELECT * FROM {table} ORDER BY id").fetchall())
            for table in self.EXPORT_TABLES
        }
