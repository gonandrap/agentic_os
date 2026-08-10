"""Per-project store: <project>/.jarvis/jarvis.db

Authoritative record of a project's work orders, their event timeline, the user⇄agent
message queue, the notification outbox, assumptions pending review, and the feature
orders that own work orders in sets.
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
    # Worker done, PR open, waiting for the user to merge it. Deliberately NOT an
    # attention item (see invariants.true_blockers): it is a merge queue the user works
    # through in the dashboard, not a decision blocking the OS, and putting every
    # finished work order in the "NEEDS YOU" strip is how that strip stops being read.
    "waiting_pr_merge",
    "completed",
    "failed",
    "cancelled",
)
OPEN_STATUSES = ("pending", "dispatching", "running", "waiting_input", "needs_review",
                 "waiting_pr_merge")
# Settled: nothing more will happen to these on their own. They are the bulk of an old
# project's history, so listings collapse them behind a count rather than printing them.
TERMINAL_STATUSES = ("completed", "cancelled", "failed")

# How the work order entered the system. jarvis/ui follow the framework; manual is a
# direct DB insert; injected is a session the user started and then handed to Jarvis
# with `jarvis wo inject`; adhoc is the legacy marker for a session the reconciler
# adopted on its own, which it no longer does (GitHub issue 47); neo is one Neo filed
# itself (a ledger cleanup), which nobody asked for by hand — worth telling apart from
# `jarvis` in listings for exactly that reason.
WO_ORIGINS = ("jarvis", "ui", "manual", "adhoc", "injected", "neo")

# Origins whose session Jarvis did not dispatch: it belongs to the user, never received
# the worker briefing or `JARVIS_WO_ID`, and therefore cannot satisfy the worker contract
# (no `jarvis wo finish`, and its ending is not a failure). Holding one to that contract
# is what made every such record a permanent attention item — see INV-ADHOC-NOT-GOVERNED.
# `neo` is deliberately NOT here: Neo files the record, but the daemon dispatches it like
# any other work order, briefing and all.
UNGOVERNED_ORIGINS = ("adhoc", "injected")

# What a work order IS to the OS, which is not the same question as what it is about.
# `worker` is every work order that has ever existed: one session, one job, one pull
# request. `planner` is the session a feature order opens to decompose itself — same
# transport, same worktree, same contract, but a different briefing and a structured
# terminal action (`jarvis fo plan`) instead of a prose one.
#
# A column rather than a derivation, even though `feature_orders.plan_wo_id` already
# names the planner: `parent_id` cannot tell the two apart (a feature order's CHILDREN
# carry it too), and the briefing has to know which it is composing without querying
# back up into a second table on every dispatch.
WO_KINDS = ("worker", "planner")

# A work order occupying a slot: dispatched, running, or waiting on something. One
# constant rather than a literal at each site, because the two readers must agree — the
# project-wide cap (`count_active`, spent by `Daemon.dispatch_pending`) and the
# per-feature cap (`claim_next_pending`, spent by `feature_orders.max_parallel`) would
# otherwise be free to mean different things by "active".
ACTIVE_STATUSES = ("dispatching", "running", "waiting_input")

# Feature order lifecycle. Deliberately NOT a copy of WO_STATUSES: a feature order never
# runs a session of its own, so most of a work order's states are meaningless for it.
FO_STATUSES = (
    "pending",      # created; the planner has not been dispatched
    "planning",     # the plan work order is running
    "plan_review",  # a plan was submitted; Neo is reviewing it, or it is escalated
    "executing",    # children dispatching / running
    "completed",    # every child settled successfully
    "failed",       # a child failed or was cancelled
    "cancelled",    # the user stopped it
)
FO_OPEN_STATUSES = ("pending", "planning", "plan_review", "executing")
FO_TERMINAL_STATUSES = ("completed", "failed", "cancelled")

# Work-order metadata key: this work order was authorised by whoever filed it, so the
# worker must not spend a round trip asking whether it may do the thing it was sent to
# do. Value: {"by": "neo", "scope": "<what is pre-approved, in words>", ...}.
PRE_APPROVED_KEY = "pre_approved"

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
-- A planned unit of work above the work order: the coarse ask the user actually has,
-- which the project plans into a dependency-ordered set of ordinary work orders before
-- any of them runs. Its children are `work_orders` rows carrying `parent_id`.
--
-- Per-project rather than central (where the backlog lives) because a feature order is
-- scoped to one project by construction, and keeping the parent in the same database as
-- its children buys one transaction, real foreign keys, and one query for a listing.
CREATE TABLE IF NOT EXISTS feature_orders (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    description TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL DEFAULT 'pending',
    origin TEXT NOT NULL DEFAULT 'jarvis',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    plan_wo_id TEXT REFERENCES work_orders(id),   -- the planner
    plan TEXT,                              -- the submitted plan, as JSON
    -- The Neo question reviewing the submitted plan. The back-link lives here rather
    -- than a `fo_id` on the question, mirroring `approvals.neo_question_id`: Neo's
    -- database is OS-wide and knows nothing about a project's tables.
    plan_question_id INTEGER,
    max_parallel INTEGER,                   -- slot cap for this feature's children (Phase 3)
    needs_attention INTEGER NOT NULL DEFAULT 0,
    attention_reason TEXT,
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
    -- The turn's exact accounting, compacted from the result JSON the `claude` CLI
    -- wrote to `outfile` (see claude_cli.derive_turn_usage): cost, tokens by class
    -- with the ephemeral 1h/5m split, per-API-call context peak, context window.
    -- The outfile stays the source of truth; this is the copy that outlives it.
    -- NULL means "not recorded" — a turn reaped before this column existed (readers
    -- lazily backfill it from the outfile while that survives) — never zero spend.
    usage_json TEXT,
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
CREATE INDEX IF NOT EXISTS idx_fo_status ON feature_orders(status);
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
        # The pull request this work order is waiting on, as reported by the worker via
        # `jarvis wo finish --pr`. Its presence is what puts the work order in
        # `waiting_pr_merge` rather than `completed`, and it is the link the user
        # follows from the dashboard to go and merge.
        "pr_url": "TEXT",
        # The last state the daemon read back from GitHub for `pr_url` (OPEN, MERGED or
        # CLOSED), written by `Daemon.poll_pull_requests`. CLOSED is the load-bearing
        # one: it is what tells `invariants.true_blockers` that a `needs_review` work
        # order is there because the pull request was shut without merging, rather than
        # because a worker went idle. Absent means "never polled".
        "pr_state": "TEXT",
        # Work orders that must finish before this one may be claimed (JSON list of
        # work-order ids). Deliberately NOT a `blocked` status: this codebase's statuses
        # are load-bearing — OPEN_STATUSES, TERMINAL_STATUSES, true_blockers and the
        # settle path all switch on them — and "blocked" is fully derivable from this
        # column plus the dependencies' statuses, so storing it would only invite drift.
        # `waiting_pr_merge` earned a status because nothing derived it; this does not.
        # Shape matches `backlog.depends_on` exactly, so the two read the same way.
        "depends_on": "TEXT NOT NULL DEFAULT '[]'",
        # The feature order this work order belongs to — its planner or one of its
        # children. NULL for a standalone work order, which is nearly all of them, and
        # the reason this whole migration is invisible to a project that never creates a
        # feature order. `ALTER TABLE ADD COLUMN` may carry a REFERENCES clause only
        # while the column defaults to NULL, which it does.
        "parent_id": "TEXT REFERENCES feature_orders(id)",
        # `worker` or `planner` — see WO_KINDS.
        "kind": "TEXT NOT NULL DEFAULT 'worker'",
    },
    "wo_turns": {
        # See the CREATE TABLE comment. Live databases already have `wo_turns`, so the
        # column only reaches them through here.
        "usage_json": "TEXT",
    },
    "approvals": {
        # Which SEAT attempted the command, when a subagent did. NULL means the session's
        # lead ran it directly, which is every gate a plain worker ever trips.
        #
        # Needed because `JARVIS_WO_ID` is per-session, not per-agent: a gate a subagent
        # trips files its request against the work order that owns the turn. That is the
        # right owner — the lead is answerable for what its team did — but without this
        # column the audit trail would say the planner attempted what its architect did.
        # `PreToolUse` carries `agent_type` for a subagent's call and omits the key
        # entirely for the lead's, so the payload can always tell the two apart.
        "agent_type": "TEXT",
    },
}

# A dependency is satisfied only when it reaches `completed` — the strict rule of the
# feature-order design. It is affordable because the merge poller landed first: the user
# merges the pull request they were going to merge anyway and the work order completes
# itself within a tick or two, so an edge costs no extra human step.
#
# `waiting_pr_merge` deliberately does NOT satisfy: the dependent's worktree is cut from
# the main working tree's HEAD, which does not yet contain an unmerged dependency's code,
# so releasing it early gives it a tree without the thing it was told to build on.
DEPENDENCY_SATISFIED_STATUS = "completed"

# ...and these are the statuses from which it can never get there. A dependency parked in
# one of them strands its dependents forever, which is the one case that has to speak up
# rather than sit quietly in `pending` (see invariants.true_blockers).
DEPENDENCY_DEAD_STATUSES = ("cancelled", "failed")


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
        status: str = "pending",
        session_id: str | None = None,
        depends_on: list[str] | None = None,
        parent_id: str | None = None,
        kind: str = "worker",
    ) -> dict[str, Any]:
        """Create a work order. `status` and `session_id` are set in the same INSERT
        rather than afterwards, because the row is visible to the daemon the instant it
        lands: a record that is `pending` for even a moment can be claimed and dispatched
        (`claim_next_pending`), which for an injected session would launch a worker into
        the user's own conversation.

        `depends_on` names work orders that must reach `completed` first. Every id is
        checked here, at the only moment a dependency edge is ever written — which is
        also why there is no cycle check: an edge may only point at a row that already
        exists, so the graph is acyclic by construction. Anything that lets an existing
        work order acquire an edge later loses that property and owes one.
        """
        assert origin in WO_ORIGINS, origin
        assert status in WO_STATUSES, status
        assert kind in WO_KINDS, kind
        wo_id = wo_id or db.new_id("wo")
        deps = list(depends_on or [])
        if wo_id in deps:
            raise ValueError(f"work order {wo_id!r} cannot depend on itself")
        for dep in deps:
            self.get_work_order(dep)  # KeyError names the id that does not exist
        if parent_id:
            self.get_feature_order(parent_id)  # same: KeyError names it
        ts = db.now()
        self.conn.execute(
            """INSERT INTO work_orders (id, title, description, status, origin,
                   created_at, updated_at, model, effort, permission_mode,
                   append_system_prompt, backlog_id, metadata, session_id, depends_on,
                   parent_id, kind)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                wo_id, title, description, status, origin, ts, ts, model, effort,
                permission_mode, append_system_prompt, backlog_id,
                db.to_json(metadata or {}), session_id, db.to_json(deps),
                parent_id, kind,
            ),
        )
        self.add_event(wo_id, "created", {"origin": origin, "depends_on": deps,
                                          **({"parent_id": parent_id} if parent_id else {}),
                                          **({"kind": kind} if kind != "worker" else {})})
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

    def dependencies(self, wo: dict[str, Any]) -> list[str]:
        """The ids this work order waits on. Decoded at the use site, not on the row.

        `depends_on` stays raw JSON on the dict, like `acknowledged_blockers` and unlike
        `backlog.depends_on` — the two stores differ here and this follows the local one.
        Work-order rows are handed around widely (`{**wo, ...}` in worker_session, the
        dashboard, the hooks), so decoding a column in place would change the shape of a
        dict a lot of code already reads.
        """
        return db.from_json(wo.get("depends_on"), []) or []

    def unfinished_dependencies(self, wo_id: str) -> list[dict[str, Any]]:
        """The dependencies still standing between this work order and dispatch.

        A dependency that has been deleted counts as unfinished rather than satisfied,
        and says so — the alternative is releasing a work order because the thing it was
        told to build on vanished, which is the worse of the two failures.
        """
        blockers = []
        for dep_id in self.dependencies(self.get_work_order(wo_id)):
            try:
                dep = self.get_work_order(dep_id)
            except KeyError:
                blockers.append({"id": dep_id, "status": "missing", "title": "?"})
                continue
            if dep["status"] != DEPENDENCY_SATISFIED_STATUS:
                blockers.append({k: dep[k] for k in ("id", "status", "title")})
        return blockers

    def drop_dependencies(self, wo_id: str, dep_ids: list[str]) -> list[str]:
        """Cut these edges. Returns the edges that remain.

        The only way an edge is ever removed, and it is always a deliberate act by the
        user: a dependency that can never complete strands its dependent, and cutting
        the edge is the alternative to cancelling work that is still wanted. Removing
        edges cannot create a cycle, so the acyclic-by-construction property that
        `create_work_order` relies on survives this.
        """
        remaining = [d for d in self.dependencies(self.get_work_order(wo_id))
                     if d not in dep_ids]
        self.update_work_order(wo_id, depends_on=db.to_json(remaining))
        self.add_event(wo_id, "dependencies_dropped",
                       {"dropped": dep_ids, "remaining": remaining})
        return remaining

    def claim_next_pending(self) -> dict[str, Any] | None:
        """Atomically claim the oldest claimable pending order (pending -> dispatching).

        Two things can make a pending work order unclaimable, and neither writes anything
        when it fires: the order is passed over and stays `pending`, because nothing about
        it has changed.

        1. **A dependency has not completed** (Phase 1). Note that this does not hold up
           the queue behind it — the filter is in the row selection, so a younger
           unblocked order is claimed while an older blocked one waits.
        2. **Its feature order is already running `max_parallel` children** (Phase 3).
           A per-feature slot cap, spent alongside the project-wide `max_concurrent`
           rather than instead of it: whichever is tighter binds. It applies only to a
           feature's `worker` children — the planner is the feature order deciding what
           its children are, not one of them, so capping it against its own children
           would be capping a feature against itself.

        For the overwhelming majority of work orders — no dependencies, no parent — both
        subqueries are vacuously true and this is the query it always was.
        """
        marks = ",".join("?" for _ in ACTIVE_STATUSES)
        cur = self.conn.execute(
            f"""UPDATE work_orders SET status='dispatching', updated_at=?
               WHERE id = (SELECT w.id FROM work_orders w
                           WHERE w.status='pending' AND w.hidden=0
                             AND NOT EXISTS (
                                 SELECT 1 FROM json_each(w.depends_on) dep
                                 WHERE NOT EXISTS (
                                     SELECT 1 FROM work_orders d
                                     WHERE d.id = dep.value AND d.status = ?
                                 )
                             )
                             AND NOT EXISTS (
                                 SELECT 1 FROM feature_orders f
                                 WHERE f.id = w.parent_id AND w.kind = 'worker'
                                   AND f.max_parallel IS NOT NULL
                                   AND f.max_parallel <= (
                                       SELECT COUNT(*) FROM work_orders s
                                       WHERE s.parent_id = f.id AND s.kind = 'worker'
                                         AND s.status IN ({marks})
                                   )
                             )
                           ORDER BY w.created_at LIMIT 1)
               RETURNING *"""
            , (db.now(), DEPENDENCY_SATISFIED_STATUS, *ACTIVE_STATUSES),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def count_active(self) -> int:
        marks = ",".join("?" for _ in ACTIVE_STATUSES)
        row = self.conn.execute(
            f"SELECT COUNT(*) c FROM work_orders WHERE status IN ({marks})",
            ACTIVE_STATUSES,
        ).fetchone()
        return row["c"]

    def count_active_children(self, fo_id: str) -> int:
        """How many of this feature order's children are occupying a slot right now.

        The number `max_parallel` is compared against, exposed so a listing can say why a
        child is waiting without re-deriving the claim query's arithmetic differently.
        """
        marks = ",".join("?" for _ in ACTIVE_STATUSES)
        row = self.conn.execute(
            f"""SELECT COUNT(*) c FROM work_orders
                WHERE parent_id=? AND kind='worker' AND status IN ({marks})""",
            (fo_id, *ACTIVE_STATUSES),
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
            # Foreign keys are on, so a feature order still pointing at this work order
            # as its planner would refuse the delete outright. Releasing the link is the
            # right move rather than cascading: the feature order is not the thing being
            # deleted, and losing the planner is a fact about it worth surviving.
            self.conn.execute(
                "UPDATE feature_orders SET plan_wo_id=NULL WHERE plan_wo_id=?", (wo_id,)
            )
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

    # -- feature orders ----------------------------------------------------------
    #
    # A feature order has no session, no turns and no messages of its own — everything
    # it does, it does through work orders. So it has no timeline table either: its
    # history is written into the timeline of whichever work order carried the step
    # (`plan_submitted` on the planner, `created` on each child), which is where anyone
    # investigating it is already looking.

    def create_feature_order(self, title: str, description: str = "",
                             origin: str = "jarvis", backlog_id: str | None = None,
                             metadata: dict[str, Any] | None = None,
                             fo_id: str | None = None,
                             max_parallel: int | None = None) -> dict[str, Any]:
        assert origin in WO_ORIGINS, origin
        assert max_parallel is None or max_parallel >= 1, max_parallel
        fo_id = fo_id or db.new_id("fo")
        ts = db.now()
        self.conn.execute(
            """INSERT INTO feature_orders (id, title, description, status, origin,
                   created_at, updated_at, backlog_id, metadata, max_parallel)
               VALUES (?,?,?,'pending',?,?,?,?,?,?)""",
            (fo_id, title, description, origin, ts, ts, backlog_id,
             db.to_json(metadata or {}), max_parallel),
        )
        return self.get_feature_order(fo_id)

    def get_feature_order(self, fo_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM feature_orders WHERE id=?", (fo_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"feature order {fo_id!r} not found in {self.db_path}")
        return dict(row)

    def list_feature_orders(self, statuses: tuple[str, ...] | None = None,
                            limit: int = 200) -> list[dict[str, Any]]:
        where = f" WHERE status IN ({','.join('?' for _ in statuses)})" if statuses else ""
        rows = self.conn.execute(
            f"SELECT * FROM feature_orders{where} ORDER BY created_at DESC LIMIT ?",
            (*(statuses or ()), limit),
        ).fetchall()
        return db.rows_to_dicts(rows)

    def flagged_feature_orders(self) -> list[dict[str, Any]]:
        """Every feature order asking for the user, whatever its status.

        Deliberately not filtered by `FO_OPEN_STATUSES`: `failed` is a SETTLED status and
        it is also the one a feature order raises its flag in — `settle_features` marks
        both in the same call. Listing only the open ones would drop the flag on the floor
        at the exact moment it means the most.
        """
        rows = self.conn.execute(
            "SELECT * FROM feature_orders WHERE needs_attention=1 ORDER BY created_at DESC"
        ).fetchall()
        return db.rows_to_dicts(rows)

    def update_feature_order(self, fo_id: str, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = db.now()
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(
            f"UPDATE feature_orders SET {cols} WHERE id=?", (*fields.values(), fo_id)
        )

    def set_feature_status(self, fo_id: str, status: str, **extra: Any) -> None:
        assert status in FO_STATUSES, status
        self.update_feature_order(fo_id, status=status, **extra)

    def flag_feature_attention(self, fo_id: str, reason: str) -> None:
        self.update_feature_order(fo_id, needs_attention=1, attention_reason=reason)

    def clear_feature_attention(self, fo_id: str) -> None:
        self.update_feature_order(fo_id, needs_attention=0, attention_reason=None)

    def feature_children(self, fo_id: str) -> list[dict[str, Any]]:
        """The feature order's child work orders, oldest first.

        Oldest first, unlike every other listing here: children are created in
        dependency order (`plans.creation_order`), so creation order IS the plan's
        order, and printing it newest-first would show the graph upside down.

        The planner is deliberately excluded — it carries `parent_id` too, because it
        belongs to the feature order as much as any child does, but it is the session
        that produced the plan rather than a piece of the work. `plan_wo_id` is how you
        reach it.
        """
        rows = self.conn.execute(
            "SELECT * FROM work_orders WHERE parent_id=? AND kind='worker' "
            "ORDER BY created_at",
            (fo_id,),
        ).fetchall()
        return db.rows_to_dicts(rows)

    def feature_order_for_question(self, question_id: int) -> dict[str, Any] | None:
        """The feature order whose plan this Neo question is reviewing, if any.

        The mirror of `approval_for_question`, and it exists for the same reason: Neo's
        database is OS-wide and knows nothing about a project's tables, so the back-link
        has to be resolved from this side.
        """
        row = self.conn.execute(
            "SELECT * FROM feature_orders WHERE plan_question_id=?", (question_id,)
        ).fetchone()
        return dict(row) if row else None

    def create_plan_children(self, fo_id: str,
                             ordered: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Turn an approved plan into work orders, in one transaction.

        `ordered` must already be in dependency order (`plans.creation_order`): each
        child's `needs` are plan-local keys, and they are resolved to real work-order ids
        as the children are created, which only works if every child follows the ones it
        needs. That constraint is not tidiness — `create_work_order` refuses an edge
        pointing at a row that does not exist yet, and that refusal is exactly what keeps
        the live dependency graph acyclic by construction. Resolving the keys as we go
        means a plan's edges are written by the same guarded path as a hand-typed
        `--depends-on`, rather than being stamped onto rows afterwards.

        All-or-nothing: a feature order holding three of its six children is worse than
        one holding none, because the three would start running against a plan that was
        never fully created.
        """
        by_key: dict[str, str] = {}
        created: list[str] = []
        self.conn.execute("BEGIN")
        try:
            for child in ordered:
                wo = self.create_work_order(
                    title=child["title"],
                    description=child["description"],
                    origin="jarvis",
                    depends_on=[by_key[k] for k in child["needs"] if k in by_key],
                    parent_id=fo_id,
                )
                by_key[child["key"]] = wo["id"]
                created.append(wo["id"])
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return [self.get_work_order(wo_id) for wo_id in created]

    def feature_summary(self) -> dict[str, Any]:
        """How many feature orders sit in each status, and how many want the user."""
        by_status = {
            r["status"]: int(r["n"]) for r in self.conn.execute(
                "SELECT status, COUNT(*) AS n FROM feature_orders GROUP BY status"
            ).fetchall()
        }
        attention = self.conn.execute(
            "SELECT COUNT(*) c FROM feature_orders WHERE needs_attention=1"
        ).fetchone()["c"]
        return {"by_status": by_status, "needs_attention": int(attention)}

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
                    num_turns: int | None = None,
                    usage_json: str | None = None) -> dict[str, Any]:
        assert state in ("done", "failed"), state
        self.conn.execute(
            """UPDATE wo_turns SET state=?, ended_at=?, result=?, error=?, cost_usd=?,
                                   num_turns=?, usage_json=? WHERE id=?""",
            (state, db.now(), result, error, cost_usd, num_turns, usage_json, turn_id),
        )
        return self.get_turn(turn_id)  # type: ignore[return-value]

    def set_turn_usage(self, turn_id: int, usage_json: str) -> None:
        """Backfill a settled turn's recorded usage (parsed late from its outfile)."""
        self.conn.execute("UPDATE wo_turns SET usage_json=? WHERE id=?",
                          (usage_json, turn_id))

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
                     max_uses: int = 3,
                     agent_type: str | None = None) -> dict[str, Any]:
        cur = self.conn.execute(
            """INSERT INTO approvals (wo_id, ts, kind, command, matched, justification,
                                      evidence, max_uses, agent_type)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (wo_id, db.now(), kind, command, matched, justification, evidence, max_uses,
             agent_type),
        )
        approval_id = int(cur.lastrowid)  # type: ignore[arg-type]
        self.add_event(wo_id, "gate_requested", {
            "approval_id": approval_id, "kind": kind, "command": command,
            # In the payload as well as the column: the timeline is read on its own, and
            # "the planner ran this" is exactly the wrong thing for it to imply.
            **({"agent_type": agent_type} if agent_type else {}),
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
