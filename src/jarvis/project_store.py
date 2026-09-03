"""Per-project store: <project>/.jarvis/jarvis.db

Authoritative record of a project's work orders, their event timeline, the user⇄agent
message queue, the notification outbox, assumptions pending review, and the feature
orders that own work orders in sets.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
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
    # The worker has claimed the job done and an independent panel is judging the
    # claim (see the validation-panel design). Ordered here rather than appended
    # because this tuple IS the order the dashboard renders status counts in, and a
    # step that happens between "waiting on the user" and "needs your review" reads
    # wrong anywhere else. Raises NO attention on its own: nobody is waiting on the
    # user while a round is open.
    "validating",
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
OPEN_STATUSES = ("pending", "dispatching", "running", "waiting_input", "validating",
                 "needs_review", "waiting_pr_merge")
# Settled: nothing more will happen to these on their own. They are the bulk of an old
# project's history, so listings collapse them behind a count rather than printing them.
TERMINAL_STATUSES = ("completed", "cancelled", "failed")

# The seat names a validation panel may be rostered with. This is the VOCABULARY, not
# the set whose markdown ships in a given build: a catalog may name a seat whose
# definition arrives in a later release, and that must still parse (the seat records a
# `failed` opinion at run time instead of refusing to boot the fleet). Mirrors
# `neo_store.SEATS`, which `catalog.py` already imports for exactly the same job, and
# lives beside the statuses rather than in `catalog.py` so the vocabulary sits with the
# store that records what the seats say.
VALIDATOR_SEATS = ("tester", "security", "architect", "maintainer", "chair")

# How a round ended. `pending` is a round still open; `failed` is the panel itself
# breaking (no seat answered), which is not the same as the work being `rejected`.
VALIDATION_OUTCOMES = ("pending", "passed", "rejected", "escalated", "failed")
# The outcomes that mean a round was JUDGED, and so that the submitter spent one of its
# `max_rounds`. `pending` and `failed` are deliberately absent: see
# `counted_validation_rounds`, which is the only thing that may count a round.
COUNTED_VALIDATION_OUTCOMES = ("passed", "rejected", "escalated")
# What one seat proposed. "" is a seat that offered none — it ran, but said nothing the
# arbiter can count.
VALIDATION_VERDICTS = ("pass", "reject", "")
# ...and whether that seat's opinion is usable at all.
VALIDATION_OPINION_STATUSES = ("ok", "abstained", "failed")

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
# `manager` is a long-lived coordinator session that owns one feature order's
# follow-through: it stays open for the whole feature, receives what its children
# report and decides what happens next, instead of finishing a job and exiting.
WO_KINDS = ("worker", "planner", "manager")

# A work order occupying a slot: dispatched, running, or waiting on something. One
# constant rather than a literal at each site, because the two readers must agree — the
# project-wide cap (`count_active`, spent by `Daemon.dispatch_pending`) and the
# per-feature cap (`claim_next_pending`, spent by `feature_orders.max_parallel`) would
# otherwise be free to mean different things by "active".
#
# `validating` counts: the work order still holds a live session the OS intends to
# resume with the panel's verdict, so it must spend a slot. Without it a project capped
# at two could pile six work orders into validation and then run five concurrent turns
# the moment a tick rejected them.
ACTIVE_STATUSES = ("dispatching", "running", "waiting_input", "validating")

# -- the message bus (see bus.py, and the validation-panel design doc) ----------------
#
# Nothing addresses anything directly: a cross-entity message is an envelope posted to a
# ROLE, and the router works out who fills it. These three tuples are that vocabulary.
#
# Module tuples, NOT SQL CHECK constraints, and deliberately so. No status column in this
# codebase carries a CHECK; every one of them is a tuple here plus an `assert` at the
# write site. More to the point, ENVELOPE_ROLES and ENVELOPE_KINDS are DESIGNED TO GROW —
# the whole justification for a bus is that a new participant costs a routing rule — and a
# CHECK on `to_role` would turn adding a role into a schema migration, which is exactly
# the cost this design exists to avoid.
ENVELOPE_ROLES = ("reviewer", "implementor", "manager")
ENVELOPE_KINDS = ("review_feedback", "deferral_request")
# queued -> delivered (a work order filled the role and was sent the message)
#        -> handled_by_router (nobody filled it and the router acted itself)
#        -> undeliverable (nobody filled it and nobody could act — see bus.deliver)
ENVELOPE_STATES = ("queued", "delivered", "handled_by_router", "undeliverable")

# Feature order lifecycle. Deliberately NOT a copy of WO_STATUSES: a feature order never
# runs a session of its own, so most of a work order's states are meaningless for it.
FO_STATUSES = (
    "pending",      # created; the planner has not been dispatched
    "planning",     # the plan work order is running
    "plan_review",  # a plan was submitted; Neo is reviewing it, or it is escalated
    "executing",    # children dispatching / running
    "validating",   # every child is done and the panel is judging the feature as a whole
    "completed",    # every child settled successfully
    "failed",       # a child failed or was cancelled
    "cancelled",    # the user stopped it
)
FO_OPEN_STATUSES = ("pending", "planning", "plan_review", "executing", "validating")
FO_TERMINAL_STATUSES = ("completed", "failed", "cancelled")

# Work-order metadata key: this work order was authorised by whoever filed it, so the
# worker must not spend a round trip asking whether it may do the thing it was sent to
# do. Value: {"by": "neo", "scope": "<what is pre-approved, in words>", ...}.
PRE_APPROVED_KEY = "pre_approved"

# Feature-order metadata key: children whose failure the user has already answered for
# with `jarvis fo resume`. Value: [{"wo_id": str, "ts": float, "note": str}] — the flat
# list of ids AND the record of why, in one key, because the two must never disagree.
#
# In `feature_orders.metadata` rather than on a work order or a feature event. The event
# path was the obvious home and cannot carry it: `ops.feature_event` returns False when a
# feature has no project manager order, which is every feature planned while
# `os.validation.enabled` was false. See docs/superpowers/specs/2026-08-29-feature-order-resume.md.
SUPERSEDED_CHILDREN_KEY = "superseded_children"

# -- the alarm's vocabulary ---------------------------------------------------------
#
# In the STORE because four surfaces have to agree on it and none of them may depend on
# another: the daemon raises, the supervisor judges, Neo answers and the timeline
# renders. §1 of docs/superpowers/specs/2026-08-31-the-supervisor.md freezes all four.
#
# Every value later sections write is declared HERE, in one pass. §1 of
# docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md: two sections
# editing the same tuple is a conflict for no reason; one section declaring them all is
# free.

ALARM_STATUSES = (
    "raised",     # on the supervisor's queue, awaiting a look
    "reviewing",  # claimed by a supervisor tick
    "acked",      # judged and answered with a note to the user
    "escalated",  # judged and handed to Neo
    "proposed",   # judged, and a remedy is waiting on a gate grant (§5, health spec)
    "skipped",    # never offered to the supervisor — backfilled history, or declined
    "failed",     # the review could not be completed; the alarm stays unresolved
)
# The supervisor reads, reports, and — from §5 — may ASK to act. It still never acts:
# `propose` requests a gate grant, and applying the remedy is `remedies.py`'s alone.
ALARM_VERDICTS = ("ack", "escalate", "propose")
ALARM_REVIEW_STATUSES = ("unreviewed", "approved", "corrected")

# What an alarm is ABOUT, and what noticed it. Both columns default, so every row
# written before the health spec reads back as exactly what it was — a cost alarm about
# a work order — without a backfill.
ALARM_SUBJECTS = ("work_order", "feature_order")
ALARM_SOURCES = ("cost", "health")

# A subject-level finding judges the unit rather than a turn, so it has no `seq` to
# carry — and `wo_alarms.seq` is NOT NULL, which `ALTER TABLE ADD COLUMN` cannot relax.
# This is the sentinel that fills it, and every surface printing a turn number renders
# it as "no turn": `turn -1` reaching the user is the failure this constant names.
NO_TURN = -1

# The `wo_events` kinds that carry an alarm's life, and their payloads:
#
#   cost_alarm       {kind, seq, reason, alarm_id}   the raise (daemon)
#   alarm_reviewed   {alarm_id, verdict, reason, note}
#   alarm_escalated  {alarm_id, neo_question_id}
#   alarm_advice     {alarm_id, neo_question_id, answer}
#   health_finding   {alarm_id, probe, subject_kind, subject_id, reason}      (§4)
#   health_reviewed  {subject_kind, subject_id, trigger, findings}            (§4)
#   remedy_proposed  {alarm_id, approval_id, remedy, argument}                (§5)
#   remedy_applied   {alarm_id, approval_id, remedy, result}                  (§5)
#   remedy_refused   {alarm_id, approval_id, remedy, reason}                  (§5)
#
# Duplicated by `timeline.ALARM_KINDS`, which is a leaf and may not import a store; a
# test asserts the two are equal, because a kind in only one of them means every deep
# link on §6's page stops resolving with no error anywhere.
#
# `cost_alarm`'s first three keys are UNCHANGED and load-bearing: they are the dedupe
# memory that makes it one alarm per turn per kind (see `Daemon.check_burning_turns`).
ALARM_EVENT_KINDS = ("cost_alarm", "alarm_reviewed", "alarm_escalated", "alarm_advice",
                     "health_finding", "health_reviewed",
                     "remedy_proposed", "remedy_applied", "remedy_refused")

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
    -- The commit the feature's work is measured against: what the repository looked
    -- like before any child ran, so a whole-feature diff can be taken later. Nullable,
    -- and nothing writes it yet.
    base_sha TEXT,
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
-- One cost alarm, with an identity. The `cost_alarm` event is still written and is
-- still the raise's dedupe memory; this is the object a supervisor claims, a verdict
-- attaches to and a URL can point at, none of which an event row can carry.
--
-- IN THIS DATABASE AND NOT `neo.db`, where Neo's questions live: an alarm is unreadable
-- without its work order's title, status, hidden and attention flags, and those are
-- `work_orders` columns here. `questions.wo_id` is a loose string with no foreign key,
-- so the fleet-wide read would keep its per-project fan-out AND gain a second database
-- — and the cascade below would become hand-maintained cleanup, which `neo_store` is
-- the standing evidence this OS gets wrong.
--
-- Everything past `reason` is written by later sections of
-- docs/superpowers/specs/2026-08-31-the-supervisor.md and is NULL/default until then.
CREATE TABLE IF NOT EXISTS wo_alarms (
    id TEXT PRIMARY KEY,                    -- 'al-' + db.new_id, like wo-/fo-
    wo_id TEXT NOT NULL REFERENCES work_orders(id) ON DELETE CASCADE,
    ts REAL NOT NULL,
    kind TEXT NOT NULL,                     -- inspection's alarm kinds
    seq INTEGER NOT NULL,                   -- the turn it judged
    reason TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'raised',  -- ALARM_STATUSES
    claimed_at REAL,
    attempts INTEGER NOT NULL DEFAULT 0,
    verdict TEXT,                           -- ALARM_VERDICTS
    verdict_reason TEXT,
    note TEXT,                              -- what the user is told, in words
    decided_at REAL,
    neo_question_id INTEGER,
    review_status TEXT NOT NULL DEFAULT 'unreviewed',  -- ALARM_REVIEW_STATUSES
    review_feedback TEXT,
    reviewed_at REAL
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
-- One judging round over one working unit — a work order or a feature order — by the
-- validation panel. ONE table for both, not two: the two loops record identical facts,
-- and two tables would mean two of every reader, two renderers, and two chances to
-- disagree about what a round is.
--
-- TWO NULLABLE FOREIGN KEYS rather than one polymorphic `subject_id`: a single id column
-- cannot carry ON DELETE CASCADE, so deleting a work order would strand its rounds. The
-- CHECK is what keeps exactly one of them set.
CREATE TABLE IF NOT EXISTS validation_rounds (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wo_id TEXT REFERENCES work_orders(id) ON DELETE CASCADE,
    fo_id TEXT REFERENCES feature_orders(id) ON DELETE CASCADE,
    round INTEGER NOT NULL,             -- 1-based, per subject
    ts REAL NOT NULL,
    fingerprint TEXT NOT NULL,          -- what was judged; a repeat means no new evidence
    summary TEXT NOT NULL DEFAULT '',
    evidence TEXT NOT NULL DEFAULT '',
    pr_url TEXT,
    -- pending | passed | rejected | escalated | failed
    outcome TEXT NOT NULL DEFAULT 'pending',
    reason TEXT NOT NULL DEFAULT '',    -- what was sent back
    -- Which configuration judged it (`os_config_versions.id`). Also in ADDED_COLUMNS —
    -- this table already ships, so a live database gets it only there.
    config_version TEXT,
    CHECK ((wo_id IS NULL) <> (fo_id IS NULL))
);
-- PARTIAL unique indexes, NOT `UNIQUE (wo_id, fo_id, round)`. SQLite treats NULLs as
-- distinct in a UNIQUE constraint, and every row here has a NULL in one of the two id
-- columns — so the three-column form would look perfectly correct and enforce NOTHING
-- AT ALL on either loop, letting duplicate rounds insert on both.
CREATE UNIQUE INDEX IF NOT EXISTS validation_rounds_wo
    ON validation_rounds(wo_id, round) WHERE wo_id IS NOT NULL;
CREATE UNIQUE INDEX IF NOT EXISTS validation_rounds_fo
    ON validation_rounds(fo_id, round) WHERE fo_id IS NOT NULL;
-- What one seat said in one round. Keyed on the ROUND, not on the subject: the nearest
-- precedent (`neo_store.panel_opinions`) keys on the question because a Neo question has
-- exactly one round of deliberation, whereas a validation has up to `max_rounds` — so
-- `UNIQUE (subject, seat)` here would silently overwrite round one's opinions with round
-- two's and leave no trace of either.
CREATE TABLE IF NOT EXISTS validation_opinions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    round_id INTEGER NOT NULL REFERENCES validation_rounds(id) ON DELETE CASCADE,
    ts REAL NOT NULL,
    seat TEXT NOT NULL,                 -- VALIDATOR_SEATS
    reply TEXT NOT NULL DEFAULT '',     -- the seat's raw reply, verbatim
    verdict TEXT NOT NULL DEFAULT '',   -- pass | reject | '' (none offered)
    status TEXT NOT NULL DEFAULT 'ok',  -- ok | abstained | failed
    model TEXT NOT NULL DEFAULT '',
    latency_ms INTEGER NOT NULL DEFAULT 0,
    UNIQUE (round_id, seat)
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
    -- The transient systemd unit the turn runs in, when it got one (systemd_units).
    -- NULL is the direct-Popen transport: a dev checkout, `start --foreground`, a host
    -- without systemd, or a spawn that fell back. Recorded rather than re-derived from
    -- the (wo, seq) naming convention, because `cancel` has to stop the unit that
    -- actually exists.
    unit TEXT,
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
-- THE MESSAGE BUS. One row is one message posted to a ROLE about a SUBJECT (a work
-- order or a feature order), delivered by the router in bus.py. The sender never names
-- a recipient and never learns who read it.
--
-- `delivered_wo_id` is written by the ROUTER and never by the sender. It is the only
-- record of who read an envelope, and a sender able to set it would be a sender coupled
-- to its recipient — the coupling this whole design exists to prevent.
--
-- The one CHECK is different in kind from the vocabulary tuples above: exactly-one-of-two
-- parents is a structural invariant that never changes and that no Python writer can be
-- trusted to re-derive.
CREATE TABLE IF NOT EXISTS envelopes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    subject_wo_id TEXT REFERENCES work_orders(id) ON DELETE CASCADE,
    subject_fo_id TEXT REFERENCES feature_orders(id) ON DELETE CASCADE,
    from_role TEXT NOT NULL,
    to_role TEXT NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT NOT NULL DEFAULT '{}',
    state TEXT NOT NULL DEFAULT 'queued',
    delivered_wo_id TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    note TEXT NOT NULL DEFAULT '',
    CHECK ((subject_wo_id IS NULL) <> (subject_fo_id IS NULL))
);
CREATE INDEX IF NOT EXISTS idx_turns_wo ON wo_turns(wo_id, seq);
CREATE INDEX IF NOT EXISTS idx_turns_state ON wo_turns(state);
CREATE INDEX IF NOT EXISTS idx_wo_status ON work_orders(status);
CREATE INDEX IF NOT EXISTS idx_events_wo ON wo_events(wo_id);
-- Both readers of this index run per project: `events_across` on every alarm surface,
-- and the alarm backfill's guard on EVERY ProjectStore open, which is every CLI
-- invocation. Neither had one before and both were full scans of the busiest table.
CREATE INDEX IF NOT EXISTS idx_events_kind ON wo_events(kind);
CREATE INDEX IF NOT EXISTS idx_alarms_wo ON wo_alarms(wo_id, ts);
CREATE INDEX IF NOT EXISTS idx_alarms_status ON wo_alarms(status);
CREATE INDEX IF NOT EXISTS idx_msgs_status ON wo_messages(status);
CREATE INDEX IF NOT EXISTS idx_notif_status ON notifications(status);
CREATE INDEX IF NOT EXISTS idx_approvals_wo ON approvals(wo_id, status);
CREATE INDEX IF NOT EXISTS idx_fo_status ON feature_orders(status);
CREATE INDEX IF NOT EXISTS idx_validation_opinions ON validation_opinions(round_id);
CREATE INDEX IF NOT EXISTS idx_envelopes_state ON envelopes(state, id);
CREATE INDEX IF NOT EXISTS idx_envelopes_subject ON envelopes(subject_wo_id, subject_fo_id);
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
        # Which section of the parent feature's spec this child implements, as the plan
        # named it (a heading number or its text). NULL for every standalone work order
        # and for the planner and manager, which own the whole feature rather than a
        # piece of it. Three readers, which is why it is a column and not re-derived from
        # the plan at each of them: the worker's prompt, the section file materialised
        # beside it, and the validation panel's evidence packet. See §1.2 of
        # docs/superpowers/specs/2026-08-29-spec-driven-feature-orders.md.
        "spec_section": "TEXT",
        # THE SEALED BILL (`bill.build`), written once the order reaches a terminal
        # status and never recomputed. The sources a bill is built from all expire:
        # Claude Code prunes session transcripts and result JSONs on its own schedule,
        # so an order costed on demand quietly SHRINKS as its evidence ages. Sealing it
        # at completion is what makes "what did this cost" answerable a year later.
        # NULL means not sealed yet — an open order, or one that completed before this
        # column existed — and those are costed live, with the shortfall named.
        "bill_json": "TEXT",
        "bill_sealed_at": "REAL",
        # The configuration in force when this work order was DISPATCHED — the id of a
        # row in `os_config_versions`. Frozen there beside model/effort/permission_mode
        # and for the same reason (dispatch.py), and NULL carries the same honesty as
        # `pr_state`: "ran before the console existed", never version 1. See
        # docs/superpowers/specs/2026-08-27-the-config-console.md §5.
        "config_version": "TEXT",
    },
    "feature_orders": {
        # Same, one level up: a feature's bill is its children's, and children can be
        # deleted. See the work_orders comment.
        "bill_json": "TEXT",
        "bill_sealed_at": "REAL",
        # See the CREATE TABLE comment. A live database already has `feature_orders`,
        # so the column only reaches it through here.
        "base_sha": "TEXT",
    },
    "wo_turns": {
        # See the CREATE TABLE comment. Live databases already have `wo_turns`, so the
        # column only reaches them through here.
        "usage_json": "TEXT",
        # WHY THE FAILURE DIAGNOSIS IS STORED AND THE PAUSE IS NOT. A pause is a verdict
        # — re-derived from the latest turn every time it is asked for, so it cannot go
        # stale (see worker_session's note, and Neo's ruling on question 83). These two
        # are the EVIDENCE that verdict is read from, and evidence has to be kept: they
        # come off the CLI's result JSON, which Claude Code prunes on its own schedule,
        # so a turn diagnosed from the file today is undiagnosable next week. Same rule
        # that earned `usage_json` its column.
        #
        # `terminal_reason` is why the CLI's query loop stopped, verbatim; api_error,
        # aborted_streaming, prompt_too_long, max_turns, completed and the rest of a
        # closed set it defines. `api_error_status` is the HTTP status when the failure
        # was an API error — 500+ is the transport and retriable, 429 is the usage
        # window. NULL for both means "not recorded", which is a turn reaped before
        # these columns existed, and never "nothing went wrong".
        "terminal_reason": "TEXT",
        "api_error_status": "INTEGER",
        # See the CREATE TABLE comment. NULL on every row written before turns moved into
        # their own units, which reads correctly as "the direct transport".
        "unit": "TEXT",
    },
    "validation_rounds": {
        # WHICH CONFIGURATION JUDGED THIS ROUND — a different question from the work
        # order's stamp, because one unit can be judged three times under three
        # configurations. The same idea as this row's `fingerprint`, about the other
        # input. Load-bearing rather than decorative: `Daemon._validate_work_order`
        # resolves its `ValidationConfig` from this id, so settling follows the version
        # the round was OPENED under and not a catalog that has since moved (§4.1). NULL
        # falls back to the live catalog. `validation_rounds` already ships, so the
        # column reaches a live database only through here.
        "config_version": "TEXT",
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
    # An alarm can name a FEATURE ORDER as its subject and a health probe as its source.
    # All four are additive with defaults and no CHECK: `_migrate` runs inside
    # `ProjectStore.__init__` — every CLI invocation and every reconcile of every
    # project, over live production databases — and a twelve-step table rebuild there is
    # not an option. So `wo_id` stays `NOT NULL` and means the CARRIER (see
    # `carrier_for_feature`), and `fo_id` carries no foreign key.
    #
    # The pairing the schema cannot express — `subject_kind == 'feature_order'` iff
    # `fo_id` — is enforced in `add_finding`, because a constraint neither the database
    # nor Python enforces is one that fails as a wrong page three weeks later.
    "wo_alarms": {
        "subject_kind": "TEXT NOT NULL DEFAULT 'work_order'",   # ALARM_SUBJECTS
        "fo_id": "TEXT",                                        # set iff feature_order
        "source": "TEXT NOT NULL DEFAULT 'cost'",               # ALARM_SOURCES
        "probe": "TEXT",                                        # the probe id (§2)
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
        self._backfill_alarms()

    def _backfill_alarms(self) -> None:
        """Give every alarm raised before `wo_alarms` existed a row of its own.

        A backfill rather than a permanent union read ("rows, plus events with no row")
        in `alarms_across`: that union would be in the one function every alarm surface
        is built on, for ever, to serve the two events the production fleet holds today.

        'ONCE' IS NOT FREE HERE. This runs inside `__init__` — every CLI invocation and
        every reconcile of every project, not once per release — so the guard is the
        `(wo_id, kind, seq)` set rebuilt from the table on each pass, not a flag. The
        count comparison above it is only a fast path off the hot road; correctness is
        the set. See §1 of docs/superpowers/specs/2026-08-31-the-supervisor.md.

        `skipped`, never `raised`: `raised` is the supervisor's work queue, and history
        landing in it would spend one model call per legacy alarm, fleet-wide, on turns
        that finished weeks ago.
        """
        legacy = self.conn.execute(
            "SELECT COUNT(*) c FROM wo_events WHERE kind='cost_alarm'").fetchone()["c"]
        have = self.conn.execute("SELECT COUNT(*) c FROM wo_alarms").fetchone()["c"]
        if legacy <= have:
            return
        known = {(r["wo_id"], r["kind"], r["seq"]) for r in
                 self.conn.execute("SELECT wo_id, kind, seq FROM wo_alarms")}
        rows = self.conn.execute(
            "SELECT * FROM wo_events WHERE kind='cost_alarm' ORDER BY ts").fetchall()
        for event in rows:
            payload = db.from_json(event["payload"], {}) or {}
            key = (event["wo_id"], str(payload.get("kind") or "unknown"),
                   int(payload.get("seq") or 0))
            if key in known:
                continue
            known.add(key)
            self.conn.execute(
                """INSERT INTO wo_alarms (id, wo_id, ts, kind, seq, reason, status)
                   VALUES (?,?,?,?,?,?,'skipped')""",
                (db.new_id("al"), key[0], event["ts"], key[1], key[2],
                 str(payload.get("reason") or "")),
            )

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
        spec_section: str | None = None,
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
                   parent_id, kind, spec_section)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                wo_id, title, description, status, origin, ts, ts, model, effort,
                permission_mode, append_system_prompt, backlog_id,
                db.to_json(metadata or {}), session_id, db.to_json(deps),
                parent_id, kind, spec_section or None,
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
        """How many work orders are spending one of this project's `max_concurrent` slots.

        **Managers are exempt, and the exemption is load-bearing.** A project manager
        order sits in `waiting_input` — an ACTIVE status — for the entire life of its
        feature, because idle between messages is what it is FOR. Counted, two features in
        flight would spend a `max_concurrent: 2` project's whole budget on two sessions
        doing nothing, `Daemon.dispatch_pending` would never claim another work order, and
        the project would stop with nothing on any surface saying why. A coordinator is
        not a piece of the work — the same reasoning that already exempts the planner from
        a feature's `max_parallel`, applied to the project-wide cap.

        INV-MANAGER-SLOTS re-derives this from live state, because a regression here is
        invisible from every other surface: nothing looks wrong when a project simply
        stops claiming.
        """
        marks = ",".join("?" for _ in ACTIVE_STATUSES)
        row = self.conn.execute(
            f"SELECT COUNT(*) c FROM work_orders "
            f"WHERE status IN ({marks}) AND kind != 'manager'",
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

    def seal_bill(self, order_id: str, payload_json: str, *, feature: bool = False,
                  at: float | None = None) -> None:
        """Freeze one order's bill.

        Written once at settle, and re-written only by `bill._upgrade_seal` — when the
        stored payload predates a field the module now computes AND recomputing today
        still sees at least as many tokens as the seal holds. Pass `at` to preserve the
        original seal time through such an upgrade: WHEN the order settled has not
        changed just because the payload was re-derived.
        """
        fields = {"bill_json": payload_json, "bill_sealed_at": at or db.now()}
        if feature:
            self.update_feature_order(order_id, **fields)
        else:
            self.update_work_order(order_id, **fields)

    def unsealed_terminal_orders(self, limit: int = 5) -> list[dict[str, Any]]:
        """Settled orders with no bill on record yet — oldest first, so a backlog drains.

        Bounded because the first tick after this ships meets every order the project
        has ever completed, and each bill reads transcripts off disk. A few per tick
        costs nothing and clears a hundred orders in an hour.
        """
        marks = ", ".join("?" for _ in TERMINAL_STATUSES)
        rows = self.conn.execute(
            f"SELECT * FROM work_orders WHERE status IN ({marks}) AND bill_json IS NULL"
            " ORDER BY updated_at LIMIT ?", (*TERMINAL_STATUSES, limit)).fetchall()
        return [dict(r) for r in rows]

    def unsealed_terminal_features(self, limit: int = 2) -> list[dict[str, Any]]:
        marks = ", ".join("?" for _ in FO_TERMINAL_STATUSES)
        rows = self.conn.execute(
            f"SELECT * FROM feature_orders WHERE status IN ({marks}) AND bill_json IS"
            " NULL ORDER BY updated_at LIMIT ?", (*FO_TERMINAL_STATUSES, limit)
        ).fetchall()
        return [dict(r) for r in rows]

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

    def feature_status_counts(self) -> dict[str, int]:
        """How many feature orders sit in each status. Counted in SQL, like
        `status_counts` and for the same reason: `list_feature_orders` is capped."""
        rows = self.conn.execute(
            "SELECT status, COUNT(*) AS n FROM feature_orders GROUP BY status"
        ).fetchall()
        return {row["status"]: int(row["n"]) for row in rows}

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
        """Move a feature order, and retire its agent type when it settles.

        The deletion lives HERE, not at the four callers that settle a feature (the
        daemon's completed and failed branches, the merge poller, `fo cancel`), because
        it is the one line every one of them passes through. A generated agent left
        behind after its feature is over is a persona a later, unrelated work order in
        the same project could be given.

        `remove_agent` never raises and the spec snapshot stays in the stored plan, so
        `jarvis fo agent <fo-id>` rebuilds it — §3 of
        docs/superpowers/specs/2026-08-29-spec-driven-feature-orders.md.
        """
        from . import specs

        assert status in FO_STATUSES, status
        self.update_feature_order(fo_id, status=status, **extra)
        if status in FO_TERMINAL_STATUSES:
            specs.remove_agent(self.project_path, fo_id)

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

        Every child carries `superseded`: True when the user answered for its failure
        with `jarvis fo resume`. ANNOTATED, NEVER FILTERED OUT — billing, cancellation
        and the child tree all still want the row, and a superseded child that vanished
        from the tree would look like one that never existed. Only the settle rule reads
        the flag (`Daemon.settle_features`).
        """
        rows = self.conn.execute(
            "SELECT * FROM work_orders WHERE parent_id=? AND kind='worker' "
            "ORDER BY created_at",
            (fo_id,),
        ).fetchall()
        answered = {s["wo_id"] for s in self.superseded_children(fo_id)}
        return [{**c, "superseded": c["id"] in answered}
                for c in db.rows_to_dicts(rows)]

    def superseded_children(self, fo_id: str) -> list[dict[str, Any]]:
        """Which of this feature's children the user has answered for, and why.

        Read straight off `feature_orders.metadata` rather than through
        `get_feature_order`, so a call for a feature that no longer exists is an empty
        list rather than a KeyError: `feature_children` is on every settle tick and every
        listing, and it has always been tolerant of an unknown id.
        """
        row = self.conn.execute(
            "SELECT metadata FROM feature_orders WHERE id=?", (fo_id,)
        ).fetchone()
        meta = db.from_json(row["metadata"], {}) if row else {}
        return list((meta or {}).get(SUPERSEDED_CHILDREN_KEY) or [])

    def supersede_children(self, fo_id: str, wo_ids: Iterable[str],
                           note: str = "") -> list[dict[str, Any]]:
        """Record that the user has answered for these children's failure.

        Idempotent on `wo_id`: resuming a feature twice must not double the record, and
        the FIRST note is the one kept — it is the one that was true when the decision
        was taken.
        """
        current = self.superseded_children(fo_id)
        known = {s["wo_id"] for s in current}
        for wo_id in wo_ids:
            if wo_id not in known:
                current.append({"wo_id": wo_id, "ts": db.now(), "note": note})
                known.add(wo_id)
        meta = db.from_json(self.get_feature_order(fo_id).get("metadata"), {}) or {}
        meta[SUPERSEDED_CHILDREN_KEY] = current
        self.update_feature_order(fo_id, metadata=db.to_json(meta))
        return current

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

    def feature_order_for_planner(self, wo_id: str) -> dict[str, Any] | None:
        """The feature order a plan question hangs off, found the way the question
        names it rather than the way the feature order points back.

        `feature_order_for_question` follows `plan_question_id`, so it goes blind the
        moment a resubmission moves that pointer — which is exactly the case
        `invariants.check_neo_escalations_are_live` has to judge. A plan question's
        `wo_id` is `plan_wo_id`, or the feature order itself when the planner is gone
        (`ops.submit_plan`), so both are matched here.
        """
        row = self.conn.execute(
            "SELECT * FROM feature_orders WHERE id=? OR plan_wo_id=?", (wo_id, wo_id)
        ).fetchone()
        return dict(row) if row else None

    def create_plan_children(self, fo_id: str,
                             ordered: list[dict[str, Any]],
                             manager: bool = False) -> list[dict[str, Any]]:
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

        `manager` adds this feature's PROJECT MANAGER ORDER to the same transaction: one
        `kind='manager'` work order that owns the feature's follow-through and is the
        addressee for anything the feature needs a human-shaped decision about — a panel
        rejection, a deferral. It is created HERE rather than by the caller afterwards for
        exactly the reason the children are created together: a feature holding children
        but no manager is a feature whose rejections have nowhere to go. It is not in the
        returned list, because that list is the plan the user reviewed and the manager is
        not a piece of the work; `manager_work_order(fo_id)` is how you reach it.

        The flag is off by default and its caller passes `os.validation.enabled`, so with
        validation disabled this is the method it always was.
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
                    spec_section=child.get("spec_section"),
                )
                by_key[child["key"]] = wo["id"]
                created.append(wo["id"])
            if manager:
                self.create_manager_order(fo_id)
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return [self.get_work_order(wo_id) for wo_id in created]

    def create_manager_order(self, fo_id: str) -> dict[str, Any]:
        """The feature's project manager order. Opens no transaction of its own.

        Called from inside `create_plan_children`'s transaction, so it must not BEGIN or
        COMMIT anything: an all-or-nothing release is the whole point of creating it here.

        The description is what a listing and `jarvis wo show` display. The manager's own
        briefing is composed at dispatch by `dispatch.build_worker_prompt`, which reads
        the feature's ask and the LIVE list of children rather than a snapshot taken now —
        a manager files further children as the feature runs, so a snapshot would be wrong
        by its second turn.
        """
        fo = self.get_feature_order(fo_id)
        return self.create_work_order(
            title=f"Manage {fo['title']}",
            description=(
                f"Own the follow-through for feature order {fo_id} ({fo['title']}).\n\n"
                f"This work order writes no product code and opens no pull request. It "
                f"receives what the feature needs decided, acts on each message, and "
                f"files ordinary work orders under the feature when something has to "
                f"change. Between messages it is idle, and that is correct."
            ),
            origin="jarvis",
            parent_id=fo_id,
            kind="manager",
        )

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

    def events_of_kind(self, wo_id: str, kind: str) -> list[dict[str, Any]]:
        """Every event of ONE kind on this work order, oldest first and UNCAPPED.

        `list_events` takes the oldest `limit` rows, which is right for a timeline and
        wrong for counting: a chatty work order would push the rows a counter cares
        about off the end and quietly report zero. The kind filter is what makes an
        uncapped read safe here.
        """
        rows = self.conn.execute(
            "SELECT * FROM wo_events WHERE wo_id=? AND kind=? ORDER BY ts",
            (wo_id, kind)).fetchall()
        return db.rows_to_dicts(rows)

    def events_across(self, kind: str, limit: int = 200) -> list[dict[str, Any]]:
        """Every event of ONE kind in the project, NEWEST first, with its work order.

        `events_of_kind` answers "what happened to this order". A fleet-wide review
        surface asks the opposite question, and without this it would have to read
        every work order to find the handful that carry the event at all.

        The work-order columns come along because the row is unreadable without them:
        an alarm is about a title and a status, not about an id. Hidden orders are
        included and marked — hiding takes an order out of the listings that compete
        for attention, and this page is the record of what it cost, not a listing.
        """
        rows = self.conn.execute(
            "SELECT e.ts AS ts, e.kind AS kind, e.payload AS payload,"
            " w.id AS wo_id, w.title AS title, w.status AS status,"
            " w.hidden AS hidden, w.needs_attention AS needs_attention,"
            " w.attention_reason AS attention_reason"
            " FROM wo_events e JOIN work_orders w ON w.id = e.wo_id"
            " WHERE e.kind=? ORDER BY e.ts DESC LIMIT ?", (kind, limit)).fetchall()
        return db.rows_to_dicts(rows)

    # -- cost alarms ---------------------------------------------------------

    def add_alarm(self, wo_id: str, kind: str, seq: int, reason: str) -> dict[str, Any]:
        """Record one raised alarm and return it. The caller still writes the event.

        Both, not one: the row is the identity everything downstream hangs off, and the
        `cost_alarm` event remains the raise's dedupe memory and the work order's
        timeline entry. See ALARM_EVENT_KINDS for the payloads of all four kinds.
        """
        alarm_id = db.new_id("al")
        self.conn.execute(
            """INSERT INTO wo_alarms (id, wo_id, ts, kind, seq, reason)
               VALUES (?,?,?,?,?,?)""",
            (alarm_id, wo_id, db.now(), kind, int(seq), reason),
        )
        return self.get_alarm(alarm_id)

    def add_finding(self, wo_id: str, *, kind: str, reason: str, seq: int = NO_TURN,
                    source: str = "cost", probe: str | None = None,
                    subject_kind: str = "work_order",
                    fo_id: str | None = None) -> dict[str, Any]:
        """Record one finding — the general raise, of which `add_alarm` is the cost case.

        A second entry point rather than a widened `add_alarm`, because `add_alarm`'s one
        call site sits inside `Daemon.check_burning_turns`' `(kind, seq)` dedupe, which
        §1 of docs/superpowers/specs/2026-08-31-the-supervisor.md spent a section
        protecting: moved or re-signatured, every cost alarm re-raises on every reconcile
        tick for the life of the turn.

        `wo_id` is always the CARRIER. For a feature-order subject that is
        `carrier_for_feature(fo_id)`, and the pairing below is the whole of the
        constraint the schema could not carry.
        """
        assert subject_kind in ALARM_SUBJECTS, subject_kind
        assert source in ALARM_SOURCES, source
        if (subject_kind == "feature_order") != bool(fo_id):
            raise ValueError(
                f"subject_kind={subject_kind!r} and fo_id={fo_id!r} disagree: "
                "a feature_order finding needs an fo_id, and a work_order one has none")
        alarm_id = db.new_id("al")
        self.conn.execute(
            """INSERT INTO wo_alarms (id, wo_id, ts, kind, seq, reason,
                                      subject_kind, fo_id, source, probe)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (alarm_id, wo_id, db.now(), kind, int(seq), reason,
             subject_kind, fo_id, source, probe),
        )
        return self.get_alarm(alarm_id)

    def get_alarm(self, alarm_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM wo_alarms WHERE id=?", (alarm_id,)).fetchone()
        if row is None:
            raise KeyError(alarm_id)
        return dict(row)

    def alarm_for_question(self, question_id: int) -> dict[str, Any] | None:
        """The alarm this Neo question was escalated from, if any.

        The third of `approval_for_question`'s family and it exists for the same reason:
        Neo's database is OS-wide and knows nothing about a project's tables, so the
        back-link is resolved from this side.
        """
        row = self.conn.execute(
            "SELECT * FROM wo_alarms WHERE neo_question_id=?", (question_id,)).fetchone()
        return dict(row) if row else None

    def alarms_of(self, wo_id: str) -> list[dict[str, Any]]:
        """Every alarm on one work order, oldest first.

        By CARRIER, so a feature finding appears on the order that carried it — which is
        where its record belongs and where `jarvis wo show` reads it back.
        """
        return db.rows_to_dicts(self.conn.execute(
            "SELECT * FROM wo_alarms WHERE wo_id=? ORDER BY ts", (wo_id,)).fetchall())

    def alarms_for_feature(self, fo_id: str) -> list[dict[str, Any]]:
        """Every finding ABOUT one feature order, oldest first, whatever carried it.

        The counterpart of `alarms_of`: a feature reached through two carriers has its
        findings in two places, and this is the read that puts them back together.
        """
        return db.rows_to_dicts(self.conn.execute(
            "SELECT * FROM wo_alarms WHERE fo_id=? ORDER BY ts", (fo_id,)).fetchall())

    def alarms_across(self, limit: int = 200, statuses: tuple[str, ...] | None = None,
                      wo_id: str | None = None, fo_id: str | None = None,
                      sources: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        """Every alarm in the project, NEWEST first, with its work order.

        The work-order columns are the same ones `events_across` brings along and for
        the same reason — an alarm is about a title and a status, not about an id — and
        `ops.list_cost_alarms` builds its dict straight from them, so the two reads must
        not diverge. Hidden orders are included and marked: this is the record of what
        the fleet spent, not a listing competing for attention.

        `statuses` is what makes this the supervisor's work queue as well as the review
        surface's read.

        `status` IS THE WORK ORDER'S and `alarm_status` is the row's own, matching what
        `ops.list_cost_alarms` has published since PR 159. The two tables both have a
        `status`, and `SELECT a.*, w.*` would have silently handed one of them to every
        caller depending on column order, so both are spelled out.

        THE FEATURE JOIN IS UNCONDITIONAL AND `fo_id` IS A `WHERE` FILTER, NOT A JOIN
        SWITCH. `ops._find_alarm` and `ops.list_cost_alarms`' unfiltered path — which
        together feed the alarm page, the badge, `/alarms/{project}/{alarm_id}` and
        `jarvis alarms show` — never pass `fo_id`, so joining only when it is supplied
        would leave every one of those surfaces rendering the CARRIER's title.
        """
        where = ["1=1"]
        args: list[Any] = []
        if statuses:
            where.append(f"a.status IN ({','.join('?' for _ in statuses)})")
            args.extend(statuses)
        if wo_id:
            # Filtered here rather than by the caller after the fact: `limit` is applied
            # by SQLite, so a post-filter over a busy project's newest 200 could return
            # nothing for an order that has alarms.
            where.append("a.wo_id=?")
            args.append(wo_id)
        if fo_id:
            where.append("a.fo_id=?")
            args.append(fo_id)
        if sources:
            where.append(f"a.source IN ({','.join('?' for _ in sources)})")
            args.extend(sources)
        rows = self.conn.execute(
            "SELECT a.id AS id, a.wo_id AS wo_id, a.ts AS ts, a.kind AS kind,"
            " a.seq AS seq, a.reason AS reason, a.status AS alarm_status,"
            " a.claimed_at AS claimed_at, a.attempts AS attempts,"
            " a.verdict AS verdict, a.verdict_reason AS verdict_reason,"
            " a.note AS note, a.decided_at AS decided_at,"
            " a.neo_question_id AS neo_question_id,"
            " a.review_status AS review_status, a.review_feedback AS review_feedback,"
            " a.reviewed_at AS reviewed_at,"
            " a.subject_kind AS subject_kind, a.fo_id AS fo_id,"
            " a.source AS source, a.probe AS probe,"
            " w.title AS title, w.status AS status, w.hidden AS hidden,"
            " w.needs_attention AS needs_attention,"
            " w.attention_reason AS attention_reason,"
            " f.title AS fo_title, f.status AS fo_status"
            " FROM wo_alarms a JOIN work_orders w ON w.id = a.wo_id"
            " LEFT JOIN feature_orders f ON f.id = a.fo_id"
            f" WHERE {' AND '.join(where)} ORDER BY a.ts DESC LIMIT ?",
            (*args, limit)).fetchall()
        return db.rows_to_dicts(rows)

    def claim_next_alarm(self) -> dict[str, Any] | None:
        """Atomically claim the OLDEST alarm still `raised`, or None.

        FIFO keeps the supervisor's byte-stable prompt prefix inside the cache TTL, and
        the oldest alarm is also the one closest to expiring unjudged.
        """
        cur = self.conn.execute(
            """UPDATE wo_alarms SET status='reviewing', claimed_at=?
               WHERE id = (SELECT id FROM wo_alarms WHERE status='raised'
                           ORDER BY ts LIMIT 1)
               RETURNING *""",
            (db.now(),),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def reclaim_stale_alarms(self, older_than: float,
                             max_attempts: int) -> dict[str, list[str]]:
        """Unstick alarms parked in `reviewing` by a drain that never finished.

        SHIPPED WITH `claim_next_alarm`, NOT AFTER IT: `NeoStore.claim_next` went out
        without its counterpart and a daemon restart mid-drain parked a question for ever
        (bl-3f5f1464). Past `older_than` a row goes back to `raised` with `attempts`
        incremented; at `max_attempts` it is `failed` instead — out of the queue, not
        looping, and still flagged.

        Both bounds come from `catalog.SupervisorConfig`. Returns
        {"requeued": [alarm id, ...], "failed": [...]}.
        """
        cutoff = db.now() - older_than
        # Give up FIRST, then re-queue — `NeoStore.reclaim_stale`'s ordering and its
        # reason: the other way round increments a row to the ceiling and then fails it
        # in the same call, spending an attempt the alarm never got to use.
        failed = [
            str(r["id"])
            for r in self.conn.execute(
                """UPDATE wo_alarms
                      SET status='failed',
                          verdict_reason='the supervisor never finished: stranded in '
                                         || 'reviewing after ' || attempts
                                         || ' reclaim attempt(s)'
                    WHERE status='reviewing' AND attempts >= ?
                      AND COALESCE(claimed_at, ts) < ?
                RETURNING id""",
                (max_attempts, cutoff),
            ).fetchall()
        ]
        requeued = [
            str(r["id"])
            for r in self.conn.execute(
                """UPDATE wo_alarms SET status='raised', claimed_at=NULL,
                                        attempts=attempts + 1
                    WHERE status='reviewing' AND COALESCE(claimed_at, ts) < ?
                RETURNING id""",
                (cutoff,),
            ).fetchall()
        ]
        return {"requeued": requeued, "failed": failed}

    def update_alarm(self, alarm_id: str, **fields: Any) -> None:
        for column, vocabulary in (("status", ALARM_STATUSES),
                                   ("verdict", ALARM_VERDICTS),
                                   ("review_status", ALARM_REVIEW_STATUSES)):
            value = fields.get(column)
            if value is not None:
                assert value in vocabulary, (column, value)
        if not fields:
            return
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(
            f"UPDATE wo_alarms SET {cols} WHERE id=?", (*fields.values(), alarm_id))

    def _this_conflict(self, wo_id: str, kind: str) -> int:
        """Events of `kind` since the last `pr_conflict_cleared` — this EPISODE's.

        Conflict state is derived from the timeline rather than kept in columns; the
        clear is the budget reset. See §4 of
        docs/superpowers/specs/2026-08-22-a-work-order-heals-its-own-pull-request.md.
        """
        rows = self.events_of_kind(wo_id, kind)
        if not rows:
            return 0
        cleared = self.events_of_kind(wo_id, "pr_conflict_cleared")
        since = cleared[-1]["ts"] if cleared else 0.0
        return sum(1 for r in rows if r["ts"] > since)

    def pr_conflict_attempts(self, wo_id: str) -> int:
        """How many times the OS has asked this worker to resolve the SAME conflict."""
        return self._this_conflict(wo_id, "pr_conflict_nudged")

    def pr_conflict_gave_up(self, wo_id: str) -> bool:
        """Has the OS already stopped trying on this conflict and said so?

        Not "are the attempts spent": this is what keeps the give-up event to one per
        episode on a work order that may be flagged for something else entirely.
        """
        return bool(self._this_conflict(wo_id, "pr_conflict_unresolved"))

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

    # -- envelopes (the message bus) --------------------------------------------

    def post_envelope(self, *, from_role: str, to_role: str, kind: str,
                      payload: dict[str, Any] | None = None,
                      subject_wo_id: str | None = None,
                      subject_fo_id: str | None = None) -> int:
        """Queue one envelope. Returns its id.

        Never resolves anything: who fills `to_role` is the router's question and it is
        asked at delivery, not here (see bus.post). `delivered_wo_id` is left NULL for
        the router to write, and there is deliberately no parameter for it.
        """
        assert from_role in ENVELOPE_ROLES, from_role
        assert to_role in ENVELOPE_ROLES, to_role
        assert kind in ENVELOPE_KINDS, kind
        if bool(subject_wo_id) == bool(subject_fo_id):
            raise ValueError("an envelope has exactly one subject: a work order or a "
                             "feature order, never both and never neither")
        cur = self.conn.execute(
            """INSERT INTO envelopes (ts, subject_wo_id, subject_fo_id, from_role,
                                      to_role, kind, payload)
               VALUES (?,?,?,?,?,?,?)""",
            (db.now(), subject_wo_id, subject_fo_id, from_role, to_role, kind,
             db.to_json(payload or {})),
        )
        return int(cur.lastrowid)

    def queued_envelopes(self, limit: int = 200) -> list[dict[str, Any]]:
        """Undelivered envelopes, oldest first.

        Ordered by `(ts, id)` rather than `ts` alone: two envelopes posted in the same
        transaction can share a timestamp to the microsecond, and the whole promise of
        this queue is that a subject's messages arrive in the order they were posted.
        """
        rows = self.conn.execute(
            "SELECT * FROM envelopes WHERE state='queued' ORDER BY ts, id LIMIT ?",
            (limit,),
        ).fetchall()
        return db.rows_to_dicts(rows)

    def mark_envelope(self, env_id: int, state: str, *,
                      delivered_wo_id: str | None = None, note: str = "") -> None:
        assert state in ENVELOPE_STATES, state
        fields: dict[str, Any] = {"state": state, "note": note}
        if delivered_wo_id is not None:
            fields["delivered_wo_id"] = delivered_wo_id
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(f"UPDATE envelopes SET {cols} WHERE id=?",
                          (*fields.values(), env_id))

    def bump_envelope_attempt(self, env_id: int) -> None:
        """Count one routing attempt, successful or not.

        Committed on its own, deliberately OUTSIDE the delivery transaction below: an
        attempt that fails rolls the delivery back, and if the count went with it a
        permanently unroutable envelope would retry for ever and INV-ENVELOPE-STUCK
        would never see it.
        """
        self.conn.execute(
            "UPDATE envelopes SET attempts = attempts + 1 WHERE id=?", (env_id,))

    def deliver_envelope(self, env_id: int, wo_id: str, content: str,
                         source: str = "bus", note: str = "") -> int:
        """Hand one envelope to a work order's message queue. ONE transaction.

        The `wo_messages` insert and the state change commit together or not at all. A
        daemon that dies between them would otherwise redeliver an envelope the worker
        has already been sent; dying before either redelivers the whole envelope, which
        is correct and is what the queue already guarantees for `jarvis wo send`.
        """
        self.conn.execute("BEGIN")
        try:
            msg_id = self.queue_message(wo_id, content, source=source)
            self.conn.execute(
                "UPDATE envelopes SET state='delivered', delivered_wo_id=?, note=? "
                "WHERE id=?", (wo_id, note, env_id))
            self.conn.execute("COMMIT")
        except Exception:
            self.conn.execute("ROLLBACK")
            raise
        return msg_id

    def envelopes(self, subject_wo_id: str | None = None,
                  subject_fo_id: str | None = None,
                  limit: int = 200) -> list[dict[str, Any]]:
        """One subject's envelopes, oldest first. No subject means all of them."""
        conds, params = [], []
        if subject_wo_id:
            conds.append("subject_wo_id=?"); params.append(subject_wo_id)
        if subject_fo_id:
            conds.append("subject_fo_id=?"); params.append(subject_fo_id)
        where = f" WHERE {' AND '.join(conds)}" if conds else ""
        rows = self.conn.execute(
            f"SELECT * FROM envelopes{where} ORDER BY ts, id LIMIT ?", (*params, limit)
        ).fetchall()
        return db.rows_to_dicts(rows)

    def manager_work_order(self, fo_id: str) -> dict[str, Any] | None:
        """The work order that owns this feature's follow-through, whatever its status.

        One is created with the children when a plan is released and
        `os.validation.enabled` is on (`create_plan_children`), so a feature planned with
        validation off has none and the router treats that as an unfilled role — which is
        every feature today. The status is NOT filtered here:
        the router has to tell "no manager was ever created" apart from "the manager was
        cancelled while its feature is still open", and those two are different verdicts.
        """
        row = self.conn.execute(
            "SELECT * FROM work_orders WHERE parent_id=? AND kind='manager' "
            "ORDER BY created_at LIMIT 1", (fo_id,)
        ).fetchone()
        return dict(row) if row else None

    def carrier_for_feature(self, fo_id: str) -> dict[str, Any] | None:
        """The work order that carries a record ABOUT this feature: manager, planner,
        newest child, or None.

        A feature order has no timeline and no alarms of its own — `wo_events.wo_id` and
        `wo_alarms.wo_id` are real foreign keys into `work_orders` — so anything said
        about a feature is recorded on whichever work order carried it. This is the
        general rule; `ops.feature_event`'s manager-only carrier is the narrow case of
        it, kept narrow because the validation loop addresses the manager specifically.
        Two rules that disagree would put a feature's record in two places.

        The order of the ladder is longest-lived first: the manager exists for the whole
        feature, the planner for its planning, a child only for its own piece. None means
        the feature has no session at all — a `pending` feature nobody has planned — and
        there is nothing to observe. §1 of
        docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md.
        """
        manager = self.manager_work_order(fo_id)
        if manager:
            return manager
        row = self.conn.execute(
            "SELECT w.* FROM feature_orders f JOIN work_orders w ON w.id = f.plan_wo_id"
            " WHERE f.id=?", (fo_id,)).fetchone()
        if row:
            return dict(row)
        # Newest, not oldest: `feature_children` is ordered by the plan's dependency
        # order, and the newest child is the one whose session is likeliest still live.
        row = self.conn.execute(
            "SELECT * FROM work_orders WHERE parent_id=? AND kind='worker'"
            " ORDER BY created_at DESC LIMIT 1", (fo_id,)).fetchone()
        return dict(row) if row else None

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

    def set_turn_pid(self, turn_id: int, pid: int | None,
                     unit: str | None = None) -> None:
        """Record how to reach the turn's process. Both halves land together: the unit
        is useless without the row and the pid alone cannot stop a cgroup."""
        self.conn.execute("UPDATE wo_turns SET pid=?, unit=? WHERE id=?",
                          (pid, unit, turn_id))

    def finish_turn(self, turn_id: int, state: str, result: str | None = None,
                    error: str | None = None, cost_usd: float | None = None,
                    num_turns: int | None = None,
                    usage_json: str | None = None,
                    terminal_reason: str | None = None,
                    api_error_status: int | None = None) -> dict[str, Any]:
        assert state in ("done", "failed"), state
        self.conn.execute(
            """UPDATE wo_turns SET state=?, ended_at=?, result=?, error=?, cost_usd=?,
                                   num_turns=?, usage_json=?, terminal_reason=?,
                                   api_error_status=? WHERE id=?""",
            (state, db.now(), result, error, cost_usd, num_turns, usage_json,
             terminal_reason or None, api_error_status, turn_id),
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

    def recent_turns(self, wo_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """The conversation's most recent turns, newest first.

        Not `list_turns(...)[-n:]`: that one's LIMIT applies to the ascending scan, so on
        a long conversation it returns the FIRST hundred turns and the tail is exactly
        what is missing. The only reader that wants the tail is the pause streak
        counter (`worker_session.pause_streak`), and it wants it cheap.
        """
        rows = self.conn.execute(
            "SELECT * FROM wo_turns WHERE wo_id=? ORDER BY seq DESC LIMIT ?",
            (wo_id, limit),
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
        n = 1 + int(self.conn.execute(
            "SELECT COUNT(*) FROM assumptions WHERE wo_id=?", (wo_id,)
        ).fetchone()[0])
        cur = self.conn.execute(
            "INSERT INTO assumptions (wo_id, ts, content) VALUES (?,?,?)",
            (wo_id, db.now(), content),
        )
        self.add_event(wo_id, "assumption", {"content": content, "n": n})
        return int(cur.lastrowid)

    def pending_assumptions(self, wo_id: str | None = None) -> list[dict[str, Any]]:
        if wo_id:
            return [a for a in self.all_assumptions(wo_id) if a["status"] == "pending"]
        # Fleet-wide view: assumptions of hidden work orders aren't asking for review.
        # No `n` here — a number only means anything beside its own work order's list.
        rows = self.conn.execute(
            """SELECT a.* FROM assumptions a JOIN work_orders w ON w.id = a.wo_id
               WHERE a.status='pending' AND w.hidden=0 ORDER BY a.ts"""
        ).fetchall()
        return db.rows_to_dicts(rows)

    def all_assumptions(self, wo_id: str) -> list[dict[str, Any]]:
        """Every assumption of a work order, reviewed or not, each numbered from 1.

        `pending_assumptions` answers "what does the user still owe a decision on";
        this answers "what was ever recorded", which is what the persistence invariant
        has to compare the timeline against.

        `n` is a position, never the row id, and is derived here rather than stored so
        that rows written before it existed still have one. See §4 of
        docs/superpowers/specs/2026-08-23-the-work-order-record.md.
        """
        rows = self.conn.execute(
            "SELECT * FROM assumptions WHERE wo_id=? ORDER BY ts, id", (wo_id,)
        ).fetchall()
        return [{**a, "n": i} for i, a in enumerate(db.rows_to_dicts(rows), start=1)]

    def review_assumption(self, assumption_id: int, status: str) -> None:
        assert status in ("accepted", "rejected"), status
        self.conn.execute("UPDATE assumptions SET status=? WHERE id=?", (status, assumption_id))

    # -- validation rounds (see the validation-panel design) ----------------------
    #
    # A round hangs off EITHER a work order or a feature order, never both and never
    # neither, so every method that names a subject takes the two as keyword-only
    # arguments and refuses anything but exactly one of them. That refusal is Python's
    # job as well as the CHECK constraint's: the constraint catches a bad INSERT, this
    # catches a bad SELECT, which would otherwise quietly return every round in the
    # project.

    @staticmethod
    def _subject(wo_id: str | None, fo_id: str | None) -> tuple[str, str]:
        """(column, id) for the one subject given. Raises if that is not exactly one."""
        if (wo_id is None) == (fo_id is None):
            raise ValueError("pass exactly one of wo_id / fo_id")
        return ("wo_id", wo_id) if wo_id is not None else ("fo_id", fo_id)  # type: ignore[return-value]

    def open_validation_round(self, *, wo_id: str | None = None,
                              fo_id: str | None = None, fingerprint: str,
                              summary: str = "", evidence: str = "",
                              pr_url: str | None = None,
                              round: int | None = None,
                              config_version: str | None = None) -> dict[str, Any]:
        """Start a round on one subject, or return the one that already holds its number.

        1-based and per subject. Left to itself the number is derived from what is
        already stored, so two rounds can never disagree about which came first; a caller
        that COUNTS rounds by outcome — the round machine does, because a `failed`
        transport round must not consume one — passes the number it counted instead.

        **Idempotent per (subject, round).** A `finish` retried while its round is still
        open, or an envelope redelivered, must not consume a second round. The
        enforcement is the partial unique index and the `IntegrityError` it raises, not a
        SELECT before the INSERT: that check-then-insert is a race with no lock, and the
        losing side would silently open the round the index exists to forbid.

        `config_version` stamps the round with the configuration it is being judged
        under; None means the ledger holds nothing yet, and reads as "not recorded".
        """
        col, subject_id = self._subject(wo_id, fo_id)
        if round is None:
            row = self.conn.execute(
                f"SELECT MAX(round) AS n FROM validation_rounds WHERE {col}=?",
                (subject_id,)).fetchone()
            round = int(row["n"] or 0) + 1
        try:
            cur = self.conn.execute(
                f"""INSERT INTO validation_rounds ({col}, round, ts, fingerprint,
                                                   summary, evidence, pr_url,
                                                   config_version)
                    VALUES (?,?,?,?,?,?,?,?)""",
                (subject_id, round, db.now(), fingerprint, summary, evidence, pr_url,
                 config_version),
            )
        except sqlite3.IntegrityError:
            existing = self.conn.execute(
                f"SELECT * FROM validation_rounds WHERE {col}=? AND round=?",
                (subject_id, round)).fetchone()
            if existing is None:  # pragma: no cover - some other constraint
                raise
            return dict(existing)
        return self.get_validation_round(int(cur.lastrowid))  # type: ignore[arg-type]

    def counted_validation_rounds(self, *, wo_id: str | None = None,
                                  fo_id: str | None = None) -> int:
        """How many rounds this subject has actually SPENT.

        Rounds are counted, never inferred from the row count: only a round that reached
        a verdict — `COUNTED_VALIDATION_OUTCOMES` — is one the submitter used up. A
        `failed` round is a transport outage rather than a judgement and must stay
        invisible here, or three bad nights on the network would give up on a unit
        nothing ever judged; a `pending` round is the one in flight, and counting it
        would make a retried submission consume a second.
        """
        col, subject_id = self._subject(wo_id, fo_id)
        marks = ",".join("?" * len(COUNTED_VALIDATION_OUTCOMES))
        row = self.conn.execute(
            f"SELECT COUNT(*) AS n FROM validation_rounds "
            f"WHERE {col}=? AND outcome IN ({marks})",
            (subject_id, *COUNTED_VALIDATION_OUTCOMES),
        ).fetchone()
        return int(row["n"] or 0)

    def get_validation_round(self, round_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM validation_rounds WHERE id=?", (round_id,)).fetchone()
        return dict(row) if row else None

    def close_validation_round(self, round_id: int, outcome: str,
                               reason: str = "") -> None:
        assert outcome in VALIDATION_OUTCOMES, outcome
        self.conn.execute(
            "UPDATE validation_rounds SET outcome=?, reason=? WHERE id=?",
            (outcome, reason, round_id),
        )

    def validation_rounds(self, *, wo_id: str | None = None,
                          fo_id: str | None = None) -> list[dict[str, Any]]:
        """Every round on one subject, oldest first — the order they were judged in."""
        col, subject_id = self._subject(wo_id, fo_id)
        return db.rows_to_dicts(self.conn.execute(
            f"SELECT * FROM validation_rounds WHERE {col}=? ORDER BY round",
            (subject_id,),
        ).fetchall())

    def latest_validation_round(self, *, wo_id: str | None = None,
                                fo_id: str | None = None) -> dict[str, Any] | None:
        """The most recent round on one subject, or None if it has never been judged."""
        col, subject_id = self._subject(wo_id, fo_id)
        row = self.conn.execute(
            f"SELECT * FROM validation_rounds WHERE {col}=? ORDER BY round DESC LIMIT 1",
            (subject_id,),
        ).fetchone()
        return dict(row) if row else None

    def record_validation_opinion(self, round_id: int, seat: str, *, reply: str = "",
                                  verdict: str = "", status: str = "ok",
                                  model: str = "", latency_ms: int = 0) -> None:
        """What one seat said. Re-recording a seat REPLACES its opinion.

        A seat that is re-run — a retry after a timeout — must leave one row, not two:
        the arbiter counts verdicts, and a doubled seat would vote twice.
        """
        assert status in VALIDATION_OPINION_STATUSES, status
        assert verdict in VALIDATION_VERDICTS, verdict
        self.conn.execute(
            """INSERT INTO validation_opinions
                   (round_id, ts, seat, reply, verdict, status, model, latency_ms)
               VALUES (?,?,?,?,?,?,?,?)
               ON CONFLICT(round_id, seat) DO UPDATE SET
                   ts=excluded.ts, reply=excluded.reply, verdict=excluded.verdict,
                   status=excluded.status, model=excluded.model,
                   latency_ms=excluded.latency_ms""",
            (round_id, db.now(), seat, reply, verdict, status, model, latency_ms),
        )

    def validation_opinions(self, round_id: int) -> list[dict[str, Any]]:
        """One round's opinions, in the order the seats first reported.

        Ordered by `id` rather than `ts` so a partial re-run cannot reshuffle rows a
        reader has already seen — the same rule `neo_store.opinions` follows.
        """
        return db.rows_to_dicts(self.conn.execute(
            "SELECT * FROM validation_opinions WHERE round_id=? ORDER BY id",
            (round_id,),
        ).fetchall())

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

    def supersede_approval(self, approval_id: int, reason: str) -> dict[str, Any]:
        """Close a pending request that no longer has an answer worth giving.

        Each of the three verdicts says something about the REQUEST: approved and denied
        permit or refuse it, dismissed calls the classifier wrong about it. This says
        something about the world around it instead — the action already ran under a
        separate approval, or the work order that filed this is over — so none of the
        three would be true, and writing one of them anyway is how a record ends up
        claiming a deploy was authorised twice.

        It lands in `expired`, the one existing status that already means "never decided,
        and can no longer be", and it authorises NOTHING: the command string stays
        blocked, so a worker that retries files a fresh request and gets a real review.
        That is the property that makes superseding safe to do automatically.

        A no-op on anything already decided — a verdict is never overwritten.
        """
        approval = self.get_approval(approval_id)
        if approval is None:
            raise KeyError(f"approval {approval_id} not found")
        if approval["status"] != "pending":
            return approval
        self.conn.execute(
            """UPDATE approvals SET status='expired', decided_by='os',
                                    decision_reason=?, decided_at=?, expires_at=NULL
               WHERE id=?""",
            (reason, db.now(), approval_id),
        )
        self.add_event(approval["wo_id"], "gate_superseded", {
            "approval_id": approval_id,
            "kind": approval["kind"],
            "command": approval["command"],
            "reason": reason,
        })
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
