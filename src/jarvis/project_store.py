"""Per-project store: <project>/.jarvis/jarvis.db"""

from __future__ import annotations

import copy
import hashlib
import json
import sqlite3
from collections.abc import Callable, Collection, Iterable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from . import db, release
from .paths import project_db_path

# Work order lifecycle
WO_STATUSES = (
    "pending",       # created, waiting for the project orchestrator to pick it up
    "dispatching",   # claimed by the daemon, worker being spawned
    "running",       # worker session active
    # A `kind='manager'` order between its feature's messages (#264)
    "idle",
    "waiting_input", # worker asked something / is blocked on the user
    # The worker has claimed the job done and an independent panel is judging the claim (see the...
    "validating",
    "needs_review",  # finished but has pending assumptions or attention items
    # Worker done, PR open, waiting for the user to merge it
    "waiting_pr_merge",
    # The order spent its dollar budget (`budget.EXHAUSTED`)
    "budget_exhausted",
    "completed",
    "failed",
    "cancelled",
)
OPEN_STATUSES = ("pending", "dispatching", "running", "idle", "waiting_input",
                 "validating", "needs_review", "waiting_pr_merge", "budget_exhausted")
# Settled: nothing more will happen to these on their own
TERMINAL_STATUSES = ("completed", "cancelled", "failed")


def inbox_flag(project_path: Path, wo_id: str) -> Path:
    """Exists while a message MAY wait for this order (docs/superpowers/specs/2026-10-22-messages-delivered-mid-turn.md)"""
    return Path(project_path) / ".jarvis" / "inbox" / f"{wo_id}.pending"

#: Why an objection was taken back — ONE STRING PER OUTCOME, and these are the two (docs/superpowers/specs/2026-10-20-an-objection-is-withdrawn-before-the-order-stops.md)
OBJECTION_WITHDRAWN_REASON = "the order stopped before it could be delivered"
OBJECTION_HANDED_BACK_REASON = "the order delivered its result and the decision is yours"

#: Branch pushed, no pull request yet; payload `Authored.record()` plus `was` (docs/superpowers/specs/2026-10-12-a-pushed-branch-with-no-pull-request-is-a-state.md §4)
PR_PENDING_EVENT = "pr_pending"

#: An OS-built prompt for this work order was past `claude_cli.MAX_OS_PROMPT_CHARS` (docs/superpowers/specs/2026-09-26-bounded-model-inputs.md)
OS_PROMPT_REFUSED_EVENT = "os_prompt_refused"
#: What settles one: the next OS call for this order that actually produced something
OS_PROMPT_REFUSAL_CLEARED_BY = ("neo_answered", "validation_passed",
                                "validation_rejected", "abandoned")
# Where a PERSON may force a fresh validation round (`ops.force_validation`)
FORCEABLE_STATUSES = ("waiting_pr_merge", "needs_review")

# The seat names a validation panel may be rostered with
VALIDATOR_SEATS = ("tester", "security", "architect", "maintainer", "chair", "deliverable")
# Seated only by a kind's own roster, never by a project's (decision-orders spec §4.2)
KIND_ONLY_SEATS = ("deliverable",)

# How a round ended (docs/superpowers/specs/2026-09-17-a-round-with-nothing-to-judge.md §3)
VALIDATION_OUTCOMES = ("pending", "passed", "rejected", "escalated", "failed", "void")
# The outcomes that mean a round was JUDGED, and so that the submitter spent one of its `max_rounds`
COUNTED_VALIDATION_OUTCOMES = ("passed", "rejected", "escalated")
# The outcomes the ROUND MACHINE still owns: `pending` is in flight, `failed` is an outage it retries
RUNNABLE_VALIDATION_OUTCOMES = ("pending", "failed")
# ...and the wider set that means the panel has not finished with this unit: `rejected` adds the wait... (docs/superpowers/specs/2026-09-13-two-gates-not-a-chain.md §2.)
OPEN_VALIDATION_OUTCOMES = ("pending", "failed", "rejected")

# Why a `failed` round failed, on its `validation_failed` event
VALIDATION_HELD_CAUSE = "usage_limit"

# ...and this one is a round waiting for GITHUB to finish running the checks
VALIDATION_CI_CAUSE = "ci_pending"

#: ...and this one is a round whose seats could not AUTHENTICATE (docs/superpowers/specs/2026-09-26-the-panel-must-not-mistake-an-auth-failure-for-a-verdict.md §7)
VALIDATION_AUTH_CAUSE = "auth"

#: Every cause that means "this round is not failed, it is WAITING"
VALIDATION_HOLDING_CAUSES = frozenset({VALIDATION_HELD_CAUSE, VALIDATION_CI_CAUSE,
                                       VALIDATION_AUTH_CAUSE})

#: HOW A ROUND READS TO A PERSON: one word, one tone, one icon, for every (outcome, hold_cause) pair (#581)
VALIDATION_STANDINGS: dict[tuple[str, str | None], tuple[str, str, str]] = {
    ("passed", None): ("passed", "ok", "✓"),
    ("rejected", None): ("rejected", "warn", "◭"),
    ("escalated", None): ("escalated", "warn", "🙋"),
    # Nothing here was for a reviewer, so nobody judged and nobody is owed a decision
    ("void", None): ("voided", "muted", "∅"),
    ("failed", None): ("failed", "bad", "✗"),
    ("failed", VALIDATION_CI_CAUSE): ("waiting for CI", "active", "◑"),
    ("failed", VALIDATION_HELD_CAUSE): ("held for the usage window", "active", "◑"),
    ("failed", VALIDATION_AUTH_CAUSE): ("held for authentication", "active", "◑"),
}


def validation_standing(round_row: Any) -> tuple[str, str, str]:
    """The word, tone and icon for one round row"""
    outcome = str((round_row or {}).get("outcome") or "")
    cause = (round_row or {}).get("hold_cause") or None
    if cause not in VALIDATION_HOLDING_CAUSES:
        cause = None
    hit = (VALIDATION_STANDINGS.get((outcome, cause))
           or VALIDATION_STANDINGS.get((outcome, None)))
    return hit or (outcome, "active", "◑")


def validation_hold_until(events: Iterable[Any], round_no: int) -> float:
    """The moment this round may go again, or 0 when nothing is holding it back (#235)"""
    return validation_hold(events, round_no)[0]


def validation_hold(events: Iterable[Any], round_no: int) -> tuple[float, str]:
    """The moment above AND THE CAUSE that won it — (0.0, "") when nothing holds (#714)"""
    held, cause = 0.0, ""
    for e in events:
        payload = db.from_json(e["payload"], {})
        if (payload.get("round") == round_no
                and payload.get("cause") in VALIDATION_HOLDING_CAUSES):
            when = float(payload.get("reopens_at") or 0.0)
            if when >= held:
                held, cause = when, str(payload["cause"])
    return held, cause


def validation_ci_hold_spans(events: Iterable[Any], since: float,
                             now: float) -> list[tuple[float, float]]:
    """Every interval inside `[since, now]` the OS was holding a round for GitHub's checks (docs/superpowers/specs/2026-10-12-a-validation-round-waiting-for-ci-is-not-stuck.md)"""
    spans: list[tuple[float, float]] = []
    for e in events:
        payload = db.from_json(e["payload"], {})
        if payload.get("cause") != VALIDATION_CI_CAUSE:
            continue
        start = max(float(e["ts"]), since)
        # The hold stands until `reopens_at` and `validation_tick` goes again the moment it lapses
        end = min(float(payload.get("reopens_at") or 0.0), now)
        if end > start:
            spans.append((start, end))
    return sorted(spans)


#: The three event kinds that move a planner's pending-assumption count
PLAN_ASSUMPTION_EVENT_KINDS = ("assumption", "autoreview_accepted", "reviewed")


def plan_hold_spans(events: Iterable[Any], since: float,
                    now: float) -> list[tuple[float, float]]:
    """Every interval inside `[since, now]` a feature's plan was unratified"""
    spans: list[tuple[float, float]] = []
    pending, opened = 0, 0.0
    for e in sorted(events, key=lambda r: float(r["ts"])):
        kind = str(e["kind"])
        if kind == "assumption":
            delta = 1
        elif kind == "autoreview_accepted":
            delta = -1
        elif kind == "reviewed":
            delta = -int(db.from_json(e["payload"], {}).get("count") or 0)
        else:
            continue
        before = pending
        # CLAMPED AT ZERO: a malformed `count` must not drive the walk negative
        pending = max(0, pending + delta)
        if before == 0 and pending > 0:
            opened = float(e["ts"])
        elif before > 0 and pending == 0:
            spans.append((opened, float(e["ts"])))
    if pending > 0:
        spans.append((opened, now))
    return sorted((max(start, since), min(end, now)) for start, end in spans
                  if min(end, now) > max(start, since))


def precondition_hold_spans(rows: Iterable[Any], since: float,
                            now: float) -> list[tuple[float, float]]:
    """Every interval inside `[since, now]` a `preconditions` row held this order"""
    spans: list[tuple[float, float]] = []
    for row in rows:
        start = max(float(row["created_at"] or 0.0), since)
        end = min(float(row["cleared_at"] or now), now)
        if end > start:
            spans.append((start, end))
    return sorted(spans)


# What one seat proposed
VALIDATION_VERDICTS = ("pass", "reject", "")
# ...and whether that seat's opinion is usable at all
VALIDATION_OPINION_STATUSES = ("ok", "abstained", "failed")

# How the work order entered the system. jarvis/ui follow the framework (docs/superpowers/specs/2026-09-30-an-order-that-stops-moving-gets-investigated.md §5)
WO_ORIGINS = ("jarvis", "ui", "manual", "adhoc", "injected", "neo", "schedule",
              "fleet_health", "os")

# Origins whose session Jarvis did not dispatch: it belongs to the user
UNGOVERNED_ORIGINS = ("adhoc", "injected")

# What a work order IS to the OS, which is not the same question as what it is about (docs/superpowers/specs/2026-09-27-investigation-orders.md)
WO_KINDS = ("worker", "planner", "manager", "analyst", "investigator", "researcher")

#: How a kind's work order settles: through `ops.finish`'s join (assumptions, panel, landing)
SETTLE_BY_FINISH = "finish"
#: …or by its submission alone, written in the session that submits it and never stopping it
SETTLE_IN_SESSION = "in_session"
#: …or never: it idles between messages until its feature settles (#264)
SETTLE_IDLE = "idle"


@dataclass(frozen=True)
class KindContract:
    """What one work-order kind hands back, and how that settles it (io-b521d17b)"""

    route: str
    #: The worktree file its contract command consumes — never stranded work
    artifact: str = ""
    #: That contract command, with `{fo_id}` for the order it reports on
    submit: str = ""
    #: May it commit, push and open a pull request?
    lands_code: bool = True
    #: Are its assumptions the user's to review?
    assumptions_reviewed: bool = True
    #: The panel that judges it, in place of the project's (decision-orders spec §4.2)
    roster: tuple[str, ...] | None = None


#: ONE ROW PER KIND, read by every submit path, `landing.authored`'s dirty set and the refusals
WO_KIND_CONTRACTS: dict[str, KindContract] = {
    "worker": KindContract(SETTLE_BY_FINISH),
    "planner": KindContract(SETTLE_BY_FINISH, artifact="plan.json",
                            submit="jarvis fo plan {fo_id} --from-file plan.json"),
    "manager": KindContract(SETTLE_IDLE),
    # By FINISH, not in session: a refused assumption is answered from the same session
    "analyst": KindContract(SETTLE_BY_FINISH, artifact="report.json",
                            submit="jarvis io report {fo_id} --from-file report.json",
                            lands_code=False),
    "investigator": KindContract(
        SETTLE_IN_SESSION, artifact="verdict.json",
        submit="jarvis investigate verdict {fo_id} --from-file verdict.json",
        lands_code=False, assumptions_reviewed=False),
    # By FINISH: an in-session kind skips the panel, and decision-orders spec §4 needs a round
    "researcher": KindContract(SETTLE_BY_FINISH, artifact="report.json",
                               submit="jarvis do report {fo_id} --from-file report.json",
                               lands_code=False, roster=("deliverable", "chair")),
}


def kind_contract(wo: Mapping[str, Any]) -> KindContract:
    """The contract row for this work order's kind"""
    return WO_KIND_CONTRACTS[str(wo.get("kind") or "worker")]


def submission_artifacts(wo: Mapping[str, Any]) -> tuple[str, ...]:
    """The file this kind's contract command consumes, as `landing.authored` excludes it"""
    artifact = kind_contract(wo).artifact
    return (artifact,) if artifact else ()

# A work order with a LIVE SESSION: dispatched, running, or parked mid-conversation on somebody else (#134)
ACTIVE_STATUSES = ("dispatching", "running", "waiting_input", "validating")

# Where a paused turn may be relaunched: every status a work order can be sitting in when... (#259)
RETRY_SWEEP_STATUSES = ("dispatching", "running", "idle", "waiting_input", "validating",
                        "needs_review", "failed", "waiting_pr_merge")

# The rest of `WO_STATUSES`, and why a due retry is not run there
NOT_RETRIED = ("pending", "completed", "cancelled", "budget_exhausted")

# What spends one of a project's `max_concurrent` slots: a work order whose turn is actually... (#134)
SLOT_STATUSES = ("dispatching", "running")

#: What a `wo_turns` row can be
TURN_KINDS = ("dispatch", "message", "compact")

#: The one whose reply belongs to nobody
COMPACT_TURN = "compact"

#: Where a turn's `cost_usd` came from — see the `wo_turns.cost_source` comment
COST_FROM_ENVELOPE = "envelope"
COST_FROM_TRANSCRIPT = "transcript"


def resume_spends_slot(wo: Mapping[str, Any]) -> bool:
    """Would starting a turn on this work order NOW take a `max_concurrent` slot it is not already holding?"""
    return wo.get("kind") != "manager" and wo["status"] not in SLOT_STATUSES

# -- the message bus (see bus.py, and the validation-panel design doc)
ENVELOPE_ROLES = ("reviewer", "implementor", "manager", "reconciler")
ENVELOPE_KINDS = ("review_feedback", "deferral_request", "children_landed",
                  "assumption_objection")
# queued -> delivered (a work order filled the role and was sent the message) -> handled_by_router...
ENVELOPE_STATES = ("queued", "delivered", "handled_by_router", "undeliverable",
                   "withdrawn")

# Feature order lifecycle
FO_STATUSES = (
    "pending",      # created; the planner has not been dispatched
    "planning",     # the plan work order is running
    "plan_review",  # a plan was submitted; Neo is reviewing it, or it is escalated
    "executing",    # children dispatching / running
    "validating",   # every child is done and the panel is judging the feature as a whole
    # A decision order's report is delivered; only the user leaves it (docs/superpowers/specs/2026-10-31-decision-orders.md §1.3)
    "awaiting_decision",
    "completed",   # every child settled successfully
    # The FAMILY budget is gone — planner
    "budget_exhausted",
    "failed",       # a child failed or was cancelled
    "cancelled",    # the user stopped it
)
FO_OPEN_STATUSES = ("pending", "planning", "plan_review", "executing", "validating",
                    "awaiting_decision", "budget_exhausted")
FO_TERMINAL_STATUSES = ("completed", "failed", "cancelled")


#: Researcher-timeline events (docs/superpowers/specs/2026-10-31-decision-orders.md §1.3)
DECISION_CLOSED_EVENT = "decision_closed_by_user"
DECISION_DELIVERED_EVENT = "decision_delivered"


class DecisionOrderGuard(RuntimeError):
    """A terminal status on a decision order from anything but the user's verb (docs/superpowers/specs/2026-10-31-decision-orders.md)"""

# What a `feature_orders` row IS (docs/superpowers/specs/2026-09-23-improvement-orders.md)
FO_KINDS = ("feature", "improvement", "investigation", "decision")

# The id prefix each kind mints, defaulting to `fo`
FO_ID_PREFIXES = {"improvement": "io", "investigation": "inv", "decision": "do"}

# Kind-aware status LABELS
FO_STATUS_LABELS: dict[str, dict[str, str]] = {
    "improvement": {"planning": "analysing", "plan_review": "findings awaiting you"},
    # No `plan_review` entry: an investigation never enters that status (§2.1 of... (docs/superpowers/specs/2026-09-27-investigation-orders.md)
    "investigation": {"planning": "investigating"},
    "decision": {"planning": "researching", "awaiting_decision": "awaiting your decision",
                 "completed": "decided"},
}


def feature_status_label(kind: str | None, status: str) -> str:
    """What to PRINT for a feature-order status, given the row's kind"""
    return FO_STATUS_LABELS.get(kind or "feature", {}).get(status, status)


#: The command that shows the user a family's decision, per kind — the CLI refuses the others
FO_DECIDE_COMMANDS = {
    "feature": "jarvis fo show {fo_id}",
    "improvement": "jarvis io review {fo_id}",
    "investigation": "jarvis investigate show {fo_id}",
    "decision": "jarvis do show {fo_id}",
}


def decide_command(kind: str | None, fo_id: str) -> str:
    """`FO_DECIDE_COMMANDS` for one row"""
    return FO_DECIDE_COMMANDS[kind or "feature"].format(fo_id=fo_id)


#: Which child kinds a family's progress counts: the work it exists to get done
FAMILY_CHILD_KINDS: dict[str, tuple[str, ...]] = {
    "feature": ("worker",),
    "improvement": ("analyst",),
    "investigation": ("investigator",),
    "decision": ("researcher",),
}

#: `feature_children(kinds=…)` for the callers that mean a feature's planned work orders
WORKER_CHILDREN: tuple[str, ...] = ("worker",)


def is_feature_order_id(unit_id: str) -> bool:
    """Does this id name a `feature_orders` row?"""
    return unit_id.startswith(("fo-", "io-", "inv-", "do-"))


def decision_report_sha(plan: str | dict | None) -> str:
    """The report's identity, blind to `filed` marks; "" for none (docs/superpowers/specs/2026-10-31-decision-orders.md)"""
    if not plan:
        return ""
    doc = json.loads(plan) if isinstance(plan, str) else copy.deepcopy(plan)
    if isinstance(doc, dict):
        for item in doc.get("proposed_orders") or []:
            if isinstance(item, dict):
                item.pop("filed", None)
    return hashlib.sha256(json.dumps(doc, sort_keys=True).encode()).hexdigest()


#: The URL segment each order id prefix is rendered under
ORDER_SEGMENTS = {"wo", "fo", *FO_ID_PREFIXES.values()}


def order_path(project: str, order_id: str) -> str:
    """The page that renders this order, from its id PREFIX (#997)"""
    prefix = order_id.split("-", 1)[0]
    # Unknown prefix: the FEATURE page, never `/wo/`
    segment = prefix if prefix in ORDER_SEGMENTS else "fo"
    return f"/{segment}/{project}/{order_id}"


# Work-order metadata key: this work order was authorised by whoever filed it
PRE_APPROVED_KEY = "pre_approved"

# Feature-order metadata key: children whose failure the user has already answered for with `jarvis... (docs/superpowers/specs/2026-08-29-feature-order-resume.md)
SUPERSEDED_CHILDREN_KEY = "superseded_children"

# -- the alarm's vocabulary

ALARM_STATUSES = (
    "raised",     # on the supervisor's queue, awaiting a look
    # A NOTE, NOT AN INTERRUPTION: recorded and rendered, never claimed
    "informational",
    "reviewing",  # claimed by a supervisor tick
    "acked",      # judged and answered with a note to the user
    "escalated",  # judged and handed to Neo
    "proposed",   # judged, and a remedy is waiting on a gate grant (§5, health spec)
    "skipped",    # never offered to the supervisor — backfilled history, or declined
    "failed",     # the review could not be completed; the alarm stays unresolved
)
# NOTHING MORE IS COMING FOR THESE: the finding was answered, noted or never offered
ALARM_SETTLED_STATUSES = ("acked", "informational", "skipped")
#: raised, reviewing, escalated, proposed, failed — a finding something still owes an answer (#1063)
ALARM_OPEN_STATUSES = tuple(s for s in ALARM_STATUSES
                            if s not in ALARM_SETTLED_STATUSES)

# The supervisor reads, reports, and — from §5 — may ASK to act
ALARM_VERDICTS = ("ack", "escalate", "propose")
ALARM_REVIEW_STATUSES = ("unreviewed", "approved", "corrected")
#: What `jarvis alarms review` may answer: a verdict, or the absence of one nothing retries (#1063)
ALARM_REVIEWABLE_STATUSES = ("acked", "escalated", "failed")

# What an alarm is ABOUT, and what noticed it
ALARM_SUBJECTS = ("work_order", "feature_order")
#: `invariant` is `invariants.check_undeclared_delivery`'s (docs/superpowers/specs/2026-09-28-stale-blockers-outlive-what-settled-them.md)
ALARM_SOURCES = ("cost", "health", "invariant", "rule")

# How one health sweep ended (`health_reviews.outcome`, §4) (docs/superpowers/specs/2026-09-28-a-usage-limit-is-not-a-failed-sweep.md)
HEALTH_OUTCOMES = ("clear", "findings", "failed", "held")

#: What a `health_reviews` row can be ABOUT
HEALTH_SUBJECTS = ALARM_SUBJECTS + ("account",)

# How long an alarm held back by a transport failure waits before it may be claimed again
ALARM_RETRY_BACKOFF_SECONDS = (60.0, 300.0, 900.0)

# How long a queued message held back by a delivery failure waits, same ladder and same reason
MESSAGE_RETRY_BACKOFF_SECONDS = ALARM_RETRY_BACKOFF_SECONDS

# How many times a message may fail to reach its worker before the user is told it is not getting...
MAX_MESSAGE_DELIVERY_ATTEMPTS = 3

# How many times launching a work order's first turn may fail on the transport before the order is...
MAX_DISPATCH_ATTEMPTS = 3
DISPATCH_RETRY_BACKOFF_SECONDS = ALARM_RETRY_BACKOFF_SECONDS

# A subject-level finding judges the unit rather than a turn, so it has no `seq` to carry
NO_TURN = -1

# The `wo_events` kinds that carry an alarm's life, and their payloads
ALARM_EVENT_KINDS = ("cost_alarm", "alarm_reviewed", "alarm_escalated", "alarm_advice",
                     "health_finding", "health_reviewed",
                     "remedy_proposed", "remedy_applied", "remedy_refused",
                     "rule_fired", "rule_dry_run", "rule_cleared")

# HOW A WORK ORDER CAN BE LINKED TO A TRACKER ISSUE, weakest first
ISSUE_LINK_KINDS = ("cited", "assigned", "raised")

#: One reclaim of a settled order's checkout — also what `settled_worktrees` excludes on (docs/superpowers/specs/2026-10-22-a-settled-checkout-is-reclaimed.md)
WORKTREE_RECLAIMED_EVENT = "worktree_reclaimed"

#: Why `claude rm` would not take a closed order's session — written once per refusal (#1257)
SESSION_KEPT_EVENT = "background_session_kept"

#: How many rows one reclaim sweep reads; `disk.MAX_PER_SWEEP` bounds the removals
MAX_PER_SWEEP_ROWS = 50

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
    metadata TEXT,
    -- WHAT A FIX ORDER CARRIES — §7.1 of
    -- docs/superpowers/specs/2026-09-27-self-evolution.md. Also in ADDED_COLUMNS, where
    -- the argument for columns over `metadata` is; this table already ships, so a live
    -- database gets all four only there.
    gap_class TEXT,
    detector_id TEXT,
    remedy_rule_id TEXT,
    trigger_wo_id TEXT
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
    -- FO_KINDS. Also in ADDED_COLUMNS, where the reasoning is.
    kind TEXT NOT NULL DEFAULT 'feature',
    origin TEXT NOT NULL DEFAULT 'jarvis',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    plan_wo_id TEXT REFERENCES work_orders(id),   -- the planner, or the ANALYST
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
    reviewed_at REAL,
    -- The remedy the supervisor proposed, its argument, and the `self_heal` approval
    -- that must be granted before `remedies.apply` may run it. Also in ADDED_COLUMNS:
    -- this table already ships, so a live database gets them only there.
    remedy TEXT,                            -- a key of remedies.REMEDIES
    remedy_argument TEXT,                   -- what to say, or why, in the judge's words
    remedy_approval_id INTEGER
);
-- THE LEDGER OF LOOKING, and it is deliberately NOT `wo_alarms` (§4 of
-- docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md). A sweep that
-- found nothing is the common case; writing it as an alarm would fill `alarms_across`,
-- `list_cost_alarms`, /alarms and `jarvis wo show`'s alarm line with rows that say
-- nothing, and every surface would then need the same filter.
--
-- No foreign key: the subject is either a work order or a feature order and neither can
-- carry a cascade for both, so `delete_work_order` deletes these rows itself. No
-- migration either — the table is new, and `CREATE TABLE IF NOT EXISTS` is what every
-- open already runs.
--
-- `fingerprint` is the DEDUPE MEMORY as well as the trigger's input: a finding is not
-- re-raised for a (subject, probe) pair already reported at the same fingerprint. That
-- is why `detail` carries the probe ids a sweep REPORTED (comma-separated) when
-- `outcome='findings'`, and the failure's reason when `outcome='failed'`.
--
-- `reopens_at` is when a `held` row's usage window lifts, and 0 on every other outcome:
-- a hold is the only row that states a future moment. Documented here rather than beside
-- the column because SQLite re-parses this text on `ALTER TABLE ... DROP COLUMN` and a
-- stranded comment makes that fail.
CREATE TABLE IF NOT EXISTS health_reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL NOT NULL,
    subject_kind TEXT NOT NULL,         -- HEALTH_SUBJECTS
    subject_id TEXT NOT NULL,
    fingerprint TEXT NOT NULL,          -- health.fingerprint
    trigger TEXT NOT NULL,              -- health.TRIGGERS
    outcome TEXT NOT NULL,              -- HEALTH_OUTCOMES
    findings INTEGER NOT NULL DEFAULT 0,
    detail TEXT NOT NULL DEFAULT '',
    reopens_at REAL NOT NULL DEFAULT 0
);
-- EVERY STATUS MOVE OF EVERY ORDER IN THIS PROJECT, work orders and feature orders in one
-- table. One table and not two because the two loops record identical facts and a reader
-- of both (`ops.state_durations`, and the fleet-health checker that consumes it) would
-- otherwise be two readers that can disagree about what a span is.
--
-- NO FOREIGN KEY ON `order_id`, and that is a consequence of the polymorphism rather than
-- laxity: the id is either a `work_orders` or a `feature_orders` row and SQLite has no
-- way to reference one of two tables from one column. The alternative shape — the nullable
-- `wo_id`/`fo_id` pair with a CHECK that `validation_rounds` uses — buys ON DELETE CASCADE
-- and costs every reader a two-column predicate; it is rejected in §"Rejected
-- alternatives" of docs/superpowers/specs/2026-09-27-time-in-each-state.md.
-- `delete_work_order` deletes these rows itself, exactly as it already does for
-- `health_reviews`.
--
-- `from_status` is '' for an order's FIRST row only (nothing preceded it).
-- `trigger` is '' whenever no caller named one, which is nearly always; the reader
-- derives a cause from the neighbouring timeline event instead — `ops.state_durations`.
-- `approximate` marks a row the BACKFILL inferred rather than observed: a feature order
-- has no event trail, so its history is one coarse span and must say so.
CREATE TABLE IF NOT EXISTS wo_state_spans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    order_id TEXT NOT NULL,
    order_kind TEXT NOT NULL,           -- 'wo' | 'fo'
    from_status TEXT NOT NULL DEFAULT '',
    to_status TEXT NOT NULL,
    ts REAL NOT NULL,
    trigger TEXT NOT NULL DEFAULT '',
    approximate INTEGER NOT NULL DEFAULT 0
);
-- One index and it is the only read pattern: every consumer asks for one order's rows
-- oldest first. `(order_kind, ts)` is NOT indexed — no surface asks "every transition in
-- this project" and an unused index is a write cost on the hot path.
CREATE INDEX IF NOT EXISTS idx_state_spans_order
    ON wo_state_spans(order_id, ts);
-- THE SCHEDULER'S CLOCK, one row per job this project runs
-- (docs/superpowers/specs/2026-09-14-the-scheduler.md §3). In the PROJECT store rather
-- than `os_state`, because what a firing produces is a project work order and two
-- projects sharing one clock would mean the first to fire silenced the rest.
--
-- `last_fired_at` IS THE ANCHOR AND IT IS NEVER NULL: seeded to the moment the job was
-- first seen enabled, so neither enabling a job nor restarting the daemon can make one
-- due. `last_wo_id IS NULL` is what tells a seeded job apart from one that has fired.
--
-- The held pair is the other half of Neo's ruling on holding silently: a job parked
-- behind its own unsettled order raises no attention, but the park is RECORDED, so
-- `jarvis doctor` can tell a scheduler that is waiting from one that is dead
-- (`invariants.check_schedule_progresses`). New table, so no migration — the
-- `CREATE TABLE IF NOT EXISTS` every open already runs is the whole upgrade.
CREATE TABLE IF NOT EXISTS scheduled_jobs (
    job_id TEXT PRIMARY KEY,            -- schedule.JOB_IDS
    created_at REAL NOT NULL,
    last_fired_at REAL NOT NULL,
    last_wo_id TEXT,                    -- no FK: the order may be deleted, the clock stays
    held_since REAL,
    held_reason TEXT
);
CREATE TABLE IF NOT EXISTS wo_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wo_id TEXT NOT NULL REFERENCES work_orders(id),
    ts REAL NOT NULL,
    direction TEXT NOT NULL,            -- user_to_agent | agent_to_user
    content TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'jarvis',  -- jarvis | ui | direct
    status TEXT NOT NULL DEFAULT 'queued',  -- queued | delivered | failed | withdrawn
    delivered_at REAL,
    authored_by TEXT NOT NULL DEFAULT ''    -- MESSAGE_AUTHOR_USER, or '' for unknown
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
-- One invariant violation the OS has already announced, and the only thing that makes
-- "report once, not every tick" (invariants.py rule 3) survive a restart. The dedupe
-- used to be a set on the Daemon object, so every release re-announced the whole
-- standing unrepaired set to Telegram — noise that scaled with release cadence (wo-31bb26ff).
CREATE TABLE IF NOT EXISTS violation_reports (
    invariant TEXT NOT NULL,
    -- '' rather than NULL for a violation that carries no work order (this is the
    -- INV-HEALTH-SWEEP-MUTE case): NULLs are DISTINCT under a SQLite primary key, so a
    -- nullable column here would open a fresh row — and fire afresh — every tick.
    wo_id TEXT NOT NULL DEFAULT '',
    first_seen REAL NOT NULL,
    last_seen REAL NOT NULL,
    seen INTEGER NOT NULL DEFAULT 1,
    -- Also in ADDED_COLUMNS, where the reasoning is: a `critical` report is one
    -- attention item for as long as it stands (Neo 1084).
    level TEXT NOT NULL DEFAULT 'warning',
    detail TEXT NOT NULL DEFAULT '',
    PRIMARY KEY (invariant, wo_id)
);
CREATE TABLE IF NOT EXISTS assumptions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wo_id TEXT NOT NULL REFERENCES work_orders(id),
    ts REAL NOT NULL,
    content TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending' -- pending | accepted | rejected | refined
    -- WHO settled it, why, under which model and configuration, and the Neo question
    -- that ruled. All in ADDED_COLUMNS, where the reasoning is — this table already
    -- ships, so a live database gets them only there. So are the PROVISIONAL verdict
    -- and the OBJECTION columns (§4.2 of the spec named in ADDED_COLUMNS), for the
    -- same reason.
);
-- ONE EXPLICIT PRECONDITION on one work order: the thing it was told not to start
-- without, as a row the claim SQL can evaluate. Written ONLY by
-- `jarvis wo create --blocked-by <ref>` and cleared only by the daemon's re-derivation or
-- by `jarvis wo unblock` — NEVER derived from the brief (kn-5d8a396a, §2.4).
--
-- `ref` is deliberately free-form text, not a foreign key: the three things a hold points
-- at live in three different databases (`bl-` in the central store, `kn-` in the central
-- store, `io-` in this one) and one of them is a tracker issue or a URL. A column that
-- could only hold what SQLite can join would refuse exactly the holds that occur.
CREATE TABLE IF NOT EXISTS preconditions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    wo_id TEXT NOT NULL REFERENCES work_orders(id) ON DELETE CASCADE,
    ref TEXT NOT NULL,                      -- bl-… | kn-… | io-… | #123 | a URL | free text
    note TEXT NOT NULL DEFAULT '',          -- the user's own words for the wait
    status TEXT NOT NULL DEFAULT 'open',    -- open | cleared
    created_at REAL NOT NULL,
    cleared_at REAL,
    cleared_by TEXT NOT NULL DEFAULT '',    -- 'os' | 'user'; '' while open
    -- Idempotence for a repeated flag, and what makes "attach this hold" re-runnable.
    UNIQUE (wo_id, ref)
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
    -- Which COMMIT was judged (the PR's headRefOid), '' when nothing binds this verdict
    -- to one. Also in ADDED_COLUMNS, where the reasoning is — this table already ships,
    -- so a live database gets it only there.
    head_sha TEXT NOT NULL DEFAULT '',
    -- Which configuration judged it (`os_config_versions.id`). Also in ADDED_COLUMNS —
    -- this table already ships, so a live database gets it only there.
    config_version TEXT,
    -- Why a PERSON opened this round by hand (`jarvis validation force`). '' is the
    -- ordinary round, opened by a submission. Also in ADDED_COLUMNS, where the
    -- reasoning is.
    forced_reason TEXT NOT NULL DEFAULT '',
    -- Whether this round is EXEMPT from `validation.max_rounds` — a rebind, the round
    -- the OS opens on a merge it asked for itself. Also in ADDED_COLUMNS, where the
    -- reasoning is (spec
    -- docs/superpowers/specs/2026-09-27-a-conflict-resolution-the-os-asked-for-costs-no-round.md §4.1).
    uncounted INTEGER NOT NULL DEFAULT 0,
    -- WHICH of the two causes exempted it — `ops.REBIND_CAUSE` or
    -- `ops.USER_REWORK_CAUSE`. '' on an ordinary round. Also in ADDED_COLUMNS, where
    -- the reasoning is.
    uncounted_cause TEXT NOT NULL DEFAULT '',
    -- WHY this round is `failed` when nothing failed: a `VALIDATION_HOLDING_CAUSES`
    -- token, NULL on every other close. Also in ADDED_COLUMNS, where the reasoning is.
    hold_cause TEXT,
    -- The commit this verdict has been CARRIED FORWARD to, and why, when the OS merged
    -- the base into the branch and moved the head itself. Separate from `head_sha`,
    -- which keeps meaning "what the seats read". Also in ADDED_COLUMNS, where the
    -- reasoning is — this table already ships, so a live database gets it only there.
    carried_head_sha TEXT NOT NULL DEFAULT '',
    carried_reason TEXT NOT NULL DEFAULT '',
    -- Per-file digests of the diff this round judged, as JSON. Also in ADDED_COLUMNS,
    -- where the reasoning is.
    file_shas TEXT NOT NULL DEFAULT '',
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
    -- awaiting_case | pending | approved | denied | dismissed | expired.
    -- `awaiting_case` is filed-but-unargued: the worker ran the command instead of
    -- asking, so no Neo question exists yet and nothing in the OS acts on the row until
    -- the worker makes its case. It is the ONLY status no reviewer can see, which is why
    -- it is also the only one with a TTL that abandons it — see gates.AWAITING_CASE.
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
    -- WHICH READING WROTE `cost_usd`. 'envelope' is the CLI's own `total_cost_usd`;
    -- 'transcript' is `usage.cost_between`, the floor derived from the session
    -- transcript for a turn whose envelope never arrived (issue #471). NULL alongside
    -- a cost is a row written before this column existed, and reads as 'envelope' —
    -- the only source there was. NULL with no cost is a turn that spent nothing on
    -- record.
    cost_source TEXT,
    num_turns INTEGER,
    -- The turn's exact accounting, compacted from the result JSON the `claude` CLI
    -- wrote to `outfile` (see claude_cli.derive_turn_usage): cost, tokens by class
    -- with the ephemeral 1h/5m split, per-API-call context peak, context window.
    -- The outfile stays the source of truth; this is the copy that outlives it.
    -- NULL means "not recorded" — a turn reaped before this column existed (readers
    -- lazily backfill it from the outfile while that survives) — never zero spend.
    usage_json TEXT,
    -- WHAT JARVIS PUT IN THIS TURN'S CONTEXT WINDOW, per ingredient: the appended system
    -- prompt, the prompt, the knowledge index, the settings file, the memory files, the
    -- persona, the --add-dir trees and the MCP server set, each with bytes and an
    -- ESTIMATED token count (`context.payload`; spec docs/superpowers/specs/
    -- 2026-09-24-order-observability.md §5). One blob per turn, same lifetime and same
    -- owner as the row, which is why it is a column and not a table.
    -- NULL means "not recorded" — a turn that ran before this landed, or one whose
    -- measurement failed — and NEVER "nothing was in the window".
    context_json TEXT,
    outfile TEXT NOT NULL DEFAULT '',
    errfile TEXT NOT NULL DEFAULT ''
);
-- THE EXACT TEXT of each context-ledger ingredient, keyed by its sha256, which the
-- ingredient row in `wo_turns.context_json` carries as `sha`. Content-addressed so an
-- ingredient that is identical on every turn (the appended prompt, the memory files)
-- is stored once. Written only by `context.record`.
CREATE TABLE IF NOT EXISTS ctx_blobs (
    sha TEXT PRIMARY KEY,
    text TEXT NOT NULL
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
    -- WHICH `wo_messages` ROW THIS ENVELOPE BECAME. In ADDED_COLUMNS, where the
    -- reasoning is — this table already ships.
    CHECK ((subject_wo_id IS NULL) <> (subject_fo_id IS NULL))
);
-- A TRACKER ISSUE THIS PROJECT IS LINKED TO, and what the OS last saw of it. Cached
-- rather than fetched, for `ops.filed_follow_ups`'s reason: a surface that asked GitHub
-- what state an issue is in would make `jarvis wo show` fail when the tracker is
-- unreachable. `state` is "" until the sweep has looked once.
CREATE TABLE IF NOT EXISTS tracked_issues (
    issue_url TEXT PRIMARY KEY,
    number INTEGER NOT NULL DEFAULT 0,
    repo TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    state TEXT NOT NULL DEFAULT '',
    checked_at REAL,
    -- The count last written to the issue as its `referenced: N` label. -1 means never,
    -- which is not the same claim as 0 and is why the default is not 0.
    refs_labelled INTEGER NOT NULL DEFAULT -1
);
-- THE RELATION THE ISSUE BODY USED TO CARRY IN PROSE ONLY. One row per (issue, unit),
-- so the reference COUNT is a count of distinct work orders — which is the signal the
-- user asked for — and an order that both raised and cites an issue counts once.
CREATE TABLE IF NOT EXISTS issue_links (
    issue_url TEXT NOT NULL,
    unit_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    round INTEGER NOT NULL DEFAULT 0,
    seat TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    -- When the comment naming this reference landed on the issue. NULL until the sweep
    -- has said it out loud; the `raised` link is stamped at creation because the filing
    -- already writes that sentence into the body.
    announced_at REAL,
    PRIMARY KEY (issue_url, unit_id)
);
-- A FOLLOW-UP THE TRACKER MAY NOT CARRY. When the OS cannot establish the project's
-- repository is private, a seat's words are not published (spec §9) — and an issue with
-- the text withheld carries nothing a reader can act on, so none is opened at all. The
-- finding lives here instead, in full, and the surfaces mark it internal-only.
-- UNIQUE on (digest, unit_id): the same dedupe key the tracker half uses, so a repeat
-- settle files nothing twice.
CREATE TABLE IF NOT EXISTS internal_follow_ups (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    digest TEXT NOT NULL,
    unit_id TEXT NOT NULL,
    round INTEGER NOT NULL DEFAULT 0,
    seat TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    detail TEXT NOT NULL DEFAULT '',
    file TEXT NOT NULL DEFAULT '',
    symbol TEXT NOT NULL DEFAULT '',
    failure TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    UNIQUE (digest, unit_id)
);
CREATE INDEX IF NOT EXISTS idx_internal_follow_ups_unit ON internal_follow_ups(unit_id);
CREATE INDEX IF NOT EXISTS idx_issue_links_unit ON issue_links(unit_id);
CREATE INDEX IF NOT EXISTS idx_turns_wo ON wo_turns(wo_id, seq);
CREATE INDEX IF NOT EXISTS idx_turns_state ON wo_turns(state);
CREATE INDEX IF NOT EXISTS idx_wo_status ON work_orders(status);
CREATE INDEX IF NOT EXISTS idx_events_wo ON wo_events(wo_id);
-- Both readers of this index run per project: `events_across` on every alarm surface,
-- and the alarm backfill's guard on EVERY ProjectStore open, which is every CLI
-- invocation. Neither had one before and both were full scans of the busiest table.
CREATE INDEX IF NOT EXISTS idx_events_kind ON wo_events(kind);
CREATE INDEX IF NOT EXISTS idx_health_subject ON health_reviews(subject_kind, subject_id, ts);
CREATE INDEX IF NOT EXISTS idx_alarms_wo ON wo_alarms(wo_id, ts);
CREATE INDEX IF NOT EXISTS idx_alarms_status ON wo_alarms(status);
CREATE INDEX IF NOT EXISTS idx_msgs_status ON wo_messages(status);
CREATE INDEX IF NOT EXISTS idx_notif_status ON notifications(status);
CREATE INDEX IF NOT EXISTS idx_approvals_wo ON approvals(wo_id, status);
CREATE INDEX IF NOT EXISTS idx_fo_status ON feature_orders(status);
-- What `count_feature_orders` counts: the fleet-health sweep's daily cap asks "how many
-- investigations did arithmetic file today", every sweep, and the listing it would
-- otherwise filter in Python walks every investigation the project has ever had
-- (docs/superpowers/specs/2026-09-30-an-order-that-stops-moving-gets-investigated.md §6c).
-- `kind` is deliberately NOT in it, though the query names it: that column arrives through
-- ADDED_COLUMNS, so a live database would run this statement before it existed.
CREATE INDEX IF NOT EXISTS idx_fo_origin_created
    ON feature_orders(origin, created_at);
CREATE INDEX IF NOT EXISTS idx_validation_opinions ON validation_opinions(round_id);
CREATE INDEX IF NOT EXISTS idx_envelopes_state ON envelopes(state, id);
CREATE INDEX IF NOT EXISTS idx_envelopes_subject ON envelopes(subject_wo_id, subject_fo_id);
-- One probe per candidate row inside the claim's own statement — §2.2 of
-- docs/superpowers/specs/2026-10-15-a-precondition-is-a-row-not-prose.md.
CREATE INDEX IF NOT EXISTS idx_preconditions_open ON preconditions(wo_id, status);
"""

# Columns added after the first release
ADDED_COLUMNS = {
    "notifications": {
        # The attention reason raised WITH this notification, carried to the central inbox (io-d2a5dd69)
        "paired_reason": "TEXT",
    },
    "work_orders": {
        # The turn transport this order's session was born on, fixed at its first launch (docs/superpowers/specs/2026-10-22-workers-on-background-sessions.md)
        "transport": "TEXT",
        # How many times launching this order's first turn has failed on the transport (docs/superpowers/specs/2026-09-18-a-failure-is-not-an-answer.md §5.)
        "dispatch_attempts": "INTEGER NOT NULL DEFAULT 0",
        "retry_after": "REAL",
        # THE DOLLAR CEILING THE USER SET on this one order, or NULL for no ceiling
        "budget_usd": "REAL",
        # ...and the slice this child's FEATURE reserved for it at dispatch
        "budget_reserved_usd": "REAL",
        # WHAT DEBUG DATA IS COLLECTED for this one order (docs/superpowers/specs/2026-09-24-order-observability.md §10)
        "observability": "TEXT",
        "job_id": "TEXT",
        "reply_job_id": "TEXT",
        # Hidden orders stay on the record but stop competing for the user's attention: out of listings
        "hidden": "INTEGER NOT NULL DEFAULT 0",
        # Blockers the user has explicitly seen and dismissed (JSON list)
        "acknowledged_blockers": "TEXT",
        # LEGACY, no longer written
        "prior_sessions": "TEXT",
        # The pull request this work order is waiting on
        "pr_url": "TEXT",
        # The last state the daemon read back from GitHub for `pr_url` (OPEN
        "pr_state": "TEXT",
        # The commit `pr_url`'s head was at the last time the poll read it, and when it was read (docs/superpowers/specs/2026-09-28-stale-blockers-outlive-what-settled-them.md)
        "pr_head_oid": "TEXT",
        "pr_head_seen_at": "REAL",
        # THE SAME CACHE FOR THE TWO FACTS THAT SAY GITHUB HAS STOPPED RECOMPUTING THIS PULL REQUEST (#995)
        "pr_ref_oid": "TEXT",
        "pr_mergeable_unknown_since": "REAL",
        # The head whose cancelled CI the OS gave up re-running, so the green path reads no event (#1123)
        "pr_checks_stuck_head": "TEXT",
        # Work orders that must finish before this one may be claimed (JSON list of work-order ids)
        "depends_on": "TEXT NOT NULL DEFAULT '[]'",
        # The feature order this work order belongs to — its planner or one of its children
        "parent_id": "TEXT REFERENCES feature_orders(id)",
        # `worker`, `planner`, `manager` or `analyst` — see WO_KINDS
        "kind": "TEXT NOT NULL DEFAULT 'worker'",
        # Which section of the parent feature's spec this child implements (docs/superpowers/specs/2026-08-29-spec-driven-feature-orders.md)
        "spec_section": "TEXT",
        # THE SEALED BILL (`bill.build`)
        "bill_json": "TEXT",
        "bill_sealed_at": "REAL",
        # THE SEALED AUTOPSY (`autopsy.to_seal`) (docs/superpowers/specs/2026-09-27-order-autopsy-durability.md)
        "autopsy_json": "TEXT",
        "autopsy_sealed_at": "REAL",
        # The configuration in force when this work order was DISPATCHED (docs/superpowers/specs/2026-08-27-the-config-console.md §5.)
        "config_version": "TEXT",
        # The tracker issue this work order exists to fix, when the OS filed it itself (`jarvis bug report`)
        "issue_url": "TEXT",
        # The priority the bug was settled at — one of `issues.PRIORITIES`
        "issue_priority": "TEXT",
        # The tracker state the OS last SUCCESSFULLY applied to `issue_url`
        "issue_state": "TEXT",
        # WHAT A FIX ORDER CARRIES: the class of gap it closes (docs/superpowers/specs/2026-09-27-self-evolution.md)
        "gap_class": "TEXT",
        "detector_id": "TEXT",
        "remedy_rule_id": "TEXT",
        "trigger_wo_id": "TEXT",
    },
    "feature_orders": {
        # Same, one level up: a feature's bill is its children's, and children can be deleted
        "bill_json": "TEXT",
        "bill_sealed_at": "REAL",
        # See the CREATE TABLE comment
        "base_sha": "TEXT",
        # A FAMILY BUDGET: the ceiling on this feature's whole rollup
        "budget_usd": "REAL",
        # WHICH KIND OF ORDER THIS ROW IS — FO_KINDS
        "kind": "TEXT NOT NULL DEFAULT 'feature'",
    },
    "wo_turns": {
        # See the CREATE TABLE comment
        "usage_json": "TEXT",
        # WHY THE FAILURE DIAGNOSIS IS STORED AND THE PAUSE IS NOT
        "terminal_reason": "TEXT",
        "api_error_status": "INTEGER",
        # See the CREATE TABLE comment
        "unit": "TEXT",
        # See the CREATE TABLE comment
        "cost_source": "TEXT",
        # See the CREATE TABLE comment
        "context_json": "TEXT",
        # WHICH CACHE-TTL ARM this turn was launched on (catalog.VALID_WORKER_CACHE_TTL) (docs/superpowers/specs/2026-10-15-the-hour-on-worker-turns-only.md)
        "cache_ttl": "TEXT NOT NULL DEFAULT ''",
        # WHAT A COMPACTION TURN ACHIEVED: a boundary, `{}` for none, NULL never read
        "compaction_json": "TEXT",
    },
    "validation_rounds": {
        # WHICH CONFIGURATION JUDGED THIS ROUND
        "config_version": "TEXT",
        # WHICH COMMIT THE PANEL JUDGED — `evidence.judged_head` (docs/superpowers/specs/2026-09-14-validated-auto-merge-design.md §5.2)
        "head_sha": "TEXT NOT NULL DEFAULT ''",
        # WHY A PERSON FORCED THIS ROUND — `jarvis validation force --reason`, verbatim (docs/superpowers/specs/2026-09-15-forcing-a-validation-round.md §3)
        "forced_reason": "TEXT NOT NULL DEFAULT ''",
        # WHETHER THIS ROUND COUNTS AGAINST `validation.max_rounds`. 1 is a REBIND: the round the OS opens on... (docs/superpowers/specs/2026-09-27-a-conflict-resolution-the-os-asked-for-costs-no-round.md)
        "uncounted": "INTEGER NOT NULL DEFAULT 0",
        # WHICH CAUSE exempted this round
        "uncounted_cause": "TEXT NOT NULL DEFAULT ''",
        # WHY A `failed` ROUND IS NOT A FAILURE
        "hold_cause": "TEXT",
        # THE COMMIT THIS VERDICT HAS BEEN CARRIED FORWARD TO, and why
        "carried_head_sha": "TEXT NOT NULL DEFAULT ''",
        "carried_reason": "TEXT NOT NULL DEFAULT ''",
        # WHAT EACH FILE LOOKED LIKE IN THE DIFF THIS ROUND JUDGED (docs/superpowers/specs/2026-09-22-a-round-must-answer-the-list.md §3)
        "file_shas": "TEXT NOT NULL DEFAULT ''",
    },
    "approvals": {
        # Which SEAT attempted the command, when a subagent did
        "agent_type": "TEXT",
        # `jarvis gate contest`: a different claim from every other row
        "contested": "INTEGER NOT NULL DEFAULT 0",
        # WHY a row landed in `expired`: 'spent', 'lapsed', 'superseded' or 'abandoned'
        "closed_as": "TEXT NOT NULL DEFAULT ''",
        # WHEN THIS REQUEST STARTED REFUSING THE WORKER'S COMMANDS (docs/superpowers/specs/2026-09-19-an-attempt-the-worker-could-not-make.md)
        "pending_at": "REAL",
        # THE BASE THIS REQUEST WAS FILED AGAINST — spec 2026-09-28 §3.5
        "base_oid": "TEXT NOT NULL DEFAULT ''",
    },
    # An alarm can name a FEATURE ORDER as its subject and a health probe as its source (docs/superpowers/specs/2026-09-28-a-usage-limit-is-not-a-failed-sweep.md §1.)
    "health_reviews": {
        "reopens_at": "REAL NOT NULL DEFAULT 0",
    },
    "wo_alarms": {
        "subject_kind": "TEXT NOT NULL DEFAULT 'work_order'",   # ALARM_SUBJECTS
        "fo_id": "TEXT",                                        # set iff feature_order
        "source": "TEXT NOT NULL DEFAULT 'cost'",               # ALARM_SOURCES
        "probe": "TEXT",                                        # the probe id (§2)
        # The proposed remedy and the gate grant that must open before it runs (§5)
        "remedy": "TEXT",
        "remedy_argument": "TEXT",
        "remedy_approval_id": "INTEGER",
        # When this alarm may next be claimed, after a review call that never happened
        "retry_after": "REAL",
    },
    # WHO WROTE THIS MESSAGE, as opposed to `source`, which is which surface filed it (docs/superpowers/specs/2026-09-11-a-gate-request-carries-the-users-words.md)
    "wo_messages": {
        "authored_by": "TEXT NOT NULL DEFAULT ''",
        # Delivery retries (docs/superpowers/specs/2026-09-18-a-failure-is-not-an-answer.md §6)
        "attempts": "INTEGER NOT NULL DEFAULT 0",
        "retry_after": "REAL",
        # Why the last delivery attempt failed, for the surface that has to say so
        "last_error": "TEXT NOT NULL DEFAULT ''",
    },
    # WHO DECIDED AN ASSUMPTION, AND ON WHAT BASIS (docs/superpowers/specs/2026-09-15-neo-decides-an-assumption.md §4)
    "assumptions": {
        "decided_by": "TEXT NOT NULL DEFAULT ''",        # '' | user | neo
        "decided_reason": "TEXT NOT NULL DEFAULT ''",    # the reviewer's one line
        # The MODEL that ruled, and the configuration it ruled under
        "decided_model": "TEXT NOT NULL DEFAULT ''",
        "decided_config_version": "TEXT",
        # The Neo question that ruled on this one assumption
        "neo_question_id": "INTEGER",
        # THE VERDICT FORMED WHILE THE WORKER WAS STILL TYPING, and it settles NOTHING
        "provisional_verdict": "TEXT NOT NULL DEFAULT ''",     # '' | accept | object
        "provisional_reason": "TEXT NOT NULL DEFAULT ''",      # Neo's one line
        # The model and the configuration behind the early verdict
        "provisional_model": "TEXT NOT NULL DEFAULT ''",
        "provisional_stakes": "TEXT NOT NULL DEFAULT ''",      # or 'unclassified'
        "provisional_ts": "REAL",
        "provisional_config_version": "TEXT",
        # THE SECOND Neo question, asked at delivery (§7)
        "confirm_question_id": "INTEGER",
        # THE OBJECTION SENT TO A RUNNING WORKER, identified by the ENVELOPE it IS
        "objection_envelope_id": "INTEGER",
        "objection_transport": "TEXT NOT NULL DEFAULT ''",     # '' | queue | peer
        # THREE TIMESTAMPS BECAUSE THEY ARE THREE FACTS
        "objection_sent_ts": "REAL",
        "objection_delivered_ts": "REAL",
        "objection_withdrawn_ts": "REAL",
        # The ordinary message that carried it LATE, when its own envelope never did (io-ce566253)
        "objection_late_msg_id": "INTEGER",
        # THE SPEC SECTIONS THIS ASSUMPTION CITES (#102)
        "spec_path": "TEXT",
        "spec_which": "TEXT",
        "spec_text": "TEXT",
    },
    # WHICH MESSAGE AN ENVELOPE BECAME
    "envelopes": {
        "delivered_msg_id": "INTEGER",
    },
    # HOW LOUD THE VIOLATION IS, and what it said (docs/superpowers/specs/2026-10-09-an-unrepairable-invariant-owes-a-decision.md §2)
    "violation_reports": {
        "level": "TEXT NOT NULL DEFAULT 'warning'",
        "detail": "TEXT NOT NULL DEFAULT ''",
        "owed": "TEXT NOT NULL DEFAULT ''",
    },
}

#: `assumptions.decided_by` when the person reviewed it
ASSUMPTION_DECIDER_USER = "user"

#: `assumptions.decided_by` when the OS did — `autoreview`
ASSUMPTION_DECIDER_OS = "neo"

#: What an early verdict may say
PROVISIONAL_VERDICTS = ("accept", "object")

#: A precondition row's two states, and the two parties that may clear one (docs/superpowers/specs/2026-10-15-a-precondition-is-a-row-not-prose.md)
PRECONDITION_STATUSES = ("open", "cleared")
PRECONDITION_CLEARED_BY = ("os", "user")

#: How an objection reached the worker
OBJECTION_TRANSPORTS = ("queue", "peer")

#: `wo_messages.authored_by` when the OS could prove the human typed it (docs/superpowers/specs/2026-09-11-a-gate-request-carries-the-users-words.md)
MESSAGE_AUTHOR_USER = "user"

# A dependency is satisfied only when it reaches `completed`
DEPENDENCY_SATISFIED_STATUS = "completed"

#: Planner with `pr_url` in any open status has its spec unmerged, rework included (docs/superpowers/specs/2026-11-01-a-held-spec-is-re-read-until-it-merges.md §1)
SPEC_PR_OPEN_STATUSES = OPEN_STATUSES

# ...and these are the statuses from which it can never get there
DEPENDENCY_DEAD_STATUSES = ("cancelled", "failed")


@dataclass(frozen=True)
class _ClaimRule:
    """One reason the claim SQL takes a row or passes it over (docs/superpowers/specs/2026-10-09-derive-somebody-is-going-to-act-on-this.md)"""

    code: str      # the column name this rule takes in the read-only query
    why: str       # one read-only sentence; no elapsed time, nothing clock-dependent
    sql: str       # boolean over alias `w`; TRUE = this rule does NOT refuse
    delayed: bool  # True = `dispatch_pending` still owns the row, its turn is just later
    params: Callable[[], tuple[Any, ...]]   # deferred: `db.now()` is read per call


#: Every refusal, in the order the claim SQL has always had them and cheapest-first
CLAIM_RULES: tuple[_ClaimRule, ...] = (
    # NOT a delay: `dispatch_pending` will never select this row at all
    _ClaimRule(
        code="selection",
        why="the claim query does not select this row: it is not `pending`, or hidden",
        sql="w.status='pending' AND w.hidden=0",
        delayed=False,
        params=lambda: (),
    ),
    # THE ORDER WAS TOLD NOT TO START, and said so structurally (docs/superpowers/specs/2026-10-15-a-precondition-is-a-row-not-prose.md)
    _ClaimRule(
        code="precondition",
        why="a precondition it was told not to start without is still open",
        sql="""NOT EXISTS (
                   SELECT 1 FROM preconditions p
                   WHERE p.wo_id = w.id AND p.status = 'open'
               )""",
        delayed=True,
        params=lambda: (),
    ),
    # A dispatch that could not reach `claude` is backing off
    _ClaimRule(
        code="retry_after",
        why="a dispatch is backing off: `retry_after` is still in the future",
        sql="COALESCE(w.retry_after, 0) <= ?",
        delayed=True,
        params=lambda: (db.now(),),
    ),
    # A dependency has not completed (Phase 1)
    _ClaimRule(
        code="dependency",
        why="a dependency has not completed yet",
        sql="""NOT EXISTS (
                   SELECT 1 FROM json_each(w.depends_on) dep
                   WHERE NOT EXISTS (
                       SELECT 1 FROM work_orders d
                       WHERE d.id = dep.value AND d.status = ?
                   )
               )""",
        delayed=True,
        params=lambda: (DEPENDENCY_SATISFIED_STATUS,),
    ),
    # Its feature order is already running `max_parallel` children (Phase 3)
    _ClaimRule(
        code="max_parallel",
        why="its feature is already running `max_parallel` children",
        sql=f"""NOT EXISTS (
                   SELECT 1 FROM feature_orders f
                   WHERE f.id = w.parent_id AND w.kind = 'worker'
                     AND f.max_parallel IS NOT NULL
                     AND f.max_parallel <= (
                         SELECT COUNT(*) FROM work_orders s
                         WHERE s.parent_id = f.id AND s.kind = 'worker'
                           AND s.status IN ({",".join("?" for _ in ACTIVE_STATUSES)})
                     )
               )""",
        delayed=True,
        params=lambda: tuple(ACTIVE_STATUSES),
    ),
    # Its feature's PLAN is still waiting on the user
    _ClaimRule(
        code="plan_assumption",
        why="its feature's plan is still waiting on the user",
        sql="""NOT (w.kind = 'worker' AND EXISTS (
                   SELECT 1 FROM feature_orders f
                   JOIN assumptions a ON a.wo_id = f.plan_wo_id
                   WHERE f.id = w.parent_id AND a.status = 'pending'
               ))""",
        delayed=True,
        params=lambda: (),
    ),
    # Its feature's SPEC pull request has not merged (docs/superpowers/specs/2026-10-29-a-feature-spec-pull-request-lands-first.md §2)
    _ClaimRule(
        code="spec_unmerged",
        why="its feature's spec pull request has not merged yet",
        sql=f"""NOT (w.kind = 'worker' AND EXISTS (
                   SELECT 1 FROM feature_orders f
                   JOIN work_orders p ON p.id = f.plan_wo_id
                   WHERE f.id = w.parent_id AND COALESCE(p.pr_url, '') != ''
                     AND p.status IN ({",".join("?" for _ in SPEC_PR_OPEN_STATUSES)})
               ))""",
        delayed=True,
        params=lambda: tuple(SPEC_PR_OPEN_STATUSES),
    ),
)


def _claim_params() -> tuple[Any, ...]:
    """Every rule's parameters, in `CLAIM_RULES` order — read fresh on every call"""
    return tuple(p for rule in CLAIM_RULES for p in rule.params())


def claim_sql_selects(store: ProjectStore, wo: dict[str, Any],
                      now: float | None = None) -> bool:
    """Would `Daemon.dispatch_pending`'s claim SQL select this row, now or later?"""
    refusal = store.claim_refusal(wo)
    return refusal is None or refusal.delayed


class ProjectStore:
    def __init__(self, project_path: str | Path):
        self.project_path = Path(project_path)
        self.db_path = project_db_path(self.project_path)
        self.conn = db.connect(self.db_path)
        self.conn.executescript(SCHEMA)
        self._migrate()

    def _migrate(self) -> None:
        db.add_missing_columns(self.conn, ADDED_COLUMNS)
        self._backfill_alarms()
        self._backfill_abandoned_gates()
        self._backfill_spent_gates()
        self._backfill_pending_at()
        self._backfill_spans()

    def _backfill_spans(self) -> None:
        """Both backfills in one write transaction: two openers must not each insert one span (kn-c23b0ce6)"""
        with db.write_transaction(self.conn):
            self._backfill_wo_spans()
            self._backfill_fo_spans()

    def _backfill_wo_spans(self) -> None:
        """Rebuild every historical work order's spans from its `kind='status'` events (docs/superpowers/specs/2026-09-27-time-in-each-state.md)"""
        total = self.conn.execute("SELECT COUNT(*) c FROM work_orders").fetchone()["c"]
        covered = self.conn.execute(
            "SELECT COUNT(DISTINCT order_id) c FROM wo_state_spans WHERE order_kind='wo'"
        ).fetchone()["c"]
        # THE HIGH-WATER GUARD, and it is what keeps this off the hot road
        newest_event = self.conn.execute(
            "SELECT MAX(ts) t FROM wo_events WHERE kind='status'").fetchone()["t"]
        newest_span = self.conn.execute(
            "SELECT MAX(ts) t FROM wo_state_spans WHERE order_kind='wo'").fetchone()["t"]
        if covered >= total and (newest_event is None
                                 or (newest_span is not None
                                     and float(newest_span) >= float(newest_event))):
            return
        # One indexed pass (`idx_events_kind`
        gaps = {r["wo_id"]: (float(r["last_span"]), str(r["tail"] or ""))
                for r in self.conn.execute(
                    """SELECT e.wo_id AS wo_id, MAX(e.ts) AS last_event,
                              (SELECT s.to_status FROM wo_state_spans s
                               WHERE s.order_id=e.wo_id AND s.order_kind='wo'
                               ORDER BY s.ts DESC, s.id DESC LIMIT 1) AS tail,
                              COALESCE((SELECT MAX(s.ts) FROM wo_state_spans s
                                        WHERE s.order_id=e.wo_id AND s.order_kind='wo'),
                                       0) AS last_span
                       FROM wo_events e WHERE e.kind='status'
                       GROUP BY e.wo_id HAVING last_event > last_span""")}
        if covered >= total and not gaps:
            return
        have = {r["order_id"] for r in self.conn.execute(
            "SELECT DISTINCT order_id FROM wo_state_spans WHERE order_kind='wo'")}
        rows = self.conn.execute(
            "SELECT id, created_at FROM work_orders").fetchall()
        for wo in rows:
            if wo["id"] in have:
                if wo["id"] not in gaps:
                    continue
                since, previous = gaps[wo["id"]]
            else:
                self._record_span(wo["id"], "wo", "", "pending", ts=wo["created_at"])
                since, previous = None, "pending"
            for event in self.conn.execute(
                    "SELECT ts, payload FROM wo_events WHERE wo_id=? AND kind='status' "
                    "ORDER BY ts, id", (wo["id"],)).fetchall():
                if since is not None and float(event["ts"]) <= float(since):
                    continue
                status = str((db.from_json(event["payload"], {}) or {}).get("status") or "")
                # The duplicate the store now prevents at the source: a zero-length span in the history would inflate...
                if not status or status == previous:
                    continue
                self._record_span(wo["id"], "wo", previous, status, ts=event["ts"])
                previous = status

    def _backfill_fo_spans(self) -> None:
        """One APPROXIMATE span per historical feature order: creation to now, as it is"""
        total = self.conn.execute("SELECT COUNT(*) c FROM feature_orders").fetchone()["c"]
        covered = self.conn.execute(
            "SELECT COUNT(DISTINCT order_id) c FROM wo_state_spans WHERE order_kind='fo'"
        ).fetchone()["c"]
        if covered >= total:
            return
        have = {r["order_id"] for r in self.conn.execute(
            "SELECT DISTINCT order_id FROM wo_state_spans WHERE order_kind='fo'")}
        for fo in self.conn.execute(
                "SELECT id, status, created_at FROM feature_orders").fetchall():
            if fo["id"] in have:
                continue
            self._record_span(fo["id"], "fo", "", str(fo["status"] or "pending"),
                              ts=fo["created_at"], approximate=True)

    def _backfill_pending_at(self) -> None:
        """Give historical rows the best `pending_at` the record can support"""
        self.conn.execute(
            """UPDATE approvals SET pending_at = ts
               WHERE pending_at IS NULL AND status != 'awaiting_case'
                 AND closed_as != 'abandoned'"""
        )
        self.conn.commit()

    #: The reason `gates.sweep_unargued` wrote while the TTL still recorded a DENIAL
    _TTL_DENIAL_PREFIX = "no case was made for it within "

    def _backfill_abandoned_gates(self) -> None:
        """Re-file the TTL's old denials as what they were: abandonments"""
        self.conn.execute(
            """UPDATE approvals SET status='expired', closed_as='abandoned'
               WHERE status='denied' AND decided_by='os' AND closed_as=''
                 AND decision_reason LIKE ?""",
            (self._TTL_DENIAL_PREFIX + "%",),
        )

    def _backfill_spent_gates(self) -> None:
        """Re-file the grants that were USED as spent, not lapsed"""
        self.conn.execute(
            """UPDATE approvals SET closed_as='spent'
               WHERE status='expired' AND closed_as='lapsed'
                 AND uses > 0 AND uses >= max_uses"""
        )

    def _backfill_alarms(self) -> None:
        """Give every alarm raised before `wo_alarms` existed a row of its own (docs/superpowers/specs/2026-08-31-the-supervisor.md)"""
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

    # -- work orders

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
        issue_url: str | None = None,
        issue_priority: str | None = None,
        budget_usd: float | None = None,
        observability: str | None = None,
        gap_class: str | None = None,
        detector_id: str | None = None,
        remedy_rule_id: str | None = None,
        trigger_wo_id: str | None = None,
    ) -> dict[str, Any]:
        """Create a work order"""
        assert origin in WO_ORIGINS, origin
        assert status in WO_STATUSES, status
        assert kind in WO_KINDS, kind
        if observability is not None:
            from . import observability as _observability  # local: see set_observability

            if observability not in _observability.LEVELS:
                raise ValueError(f"observability {observability!r} not in "
                                 f"{list(_observability.LEVELS)}")
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
                   parent_id, kind, spec_section, issue_url, issue_priority,
                   budget_usd, observability, gap_class, detector_id, remedy_rule_id,
                   trigger_wo_id)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                wo_id, title, description, status, origin, ts, ts, model, effort,
                permission_mode, append_system_prompt, backlog_id,
                db.to_json(metadata or {}), session_id, db.to_json(deps),
                parent_id, kind, spec_section or None, issue_url or None,
                issue_priority or None, budget_usd, observability,
                # `or None` as `spec_section` and `issue_url` do: "" and NULL would otherwise be two spellings of... (docs/superpowers/specs/2026-09-27-self-evolution.md)
                gap_class or None, detector_id or None, remedy_rule_id or None,
                trigger_wo_id or None,
            ),
        )
        self.add_event(wo_id, "created", {"origin": origin, "depends_on": deps,
                                          **({"parent_id": parent_id} if parent_id else {}),
                                          **({"kind": kind} if kind != "worker" else {}),
                                          **({"budget_usd": budget_usd} if budget_usd else {})})
        # The first span, which no transition can supply (docs/superpowers/specs/2026-09-27-time-in-each-state.md)
        self._record_span(wo_id, "wo", "", status, ts=ts)
        return self.get_work_order(wo_id)

    # -- preconditions

    def add_precondition(self, wo_id: str, ref: str, note: str = "") -> dict[str, Any]:
        """Attach ONE hold"""
        ref = ref.strip()
        row = self.conn.execute(
            "SELECT * FROM preconditions WHERE wo_id=? AND ref=?", (wo_id, ref)
        ).fetchone()
        if row is not None:
            return dict(row)
        self.conn.execute(
            """INSERT INTO preconditions (wo_id, ref, note, created_at)
               VALUES (?,?,?,?)""",
            (wo_id, ref, note, db.now()),
        )
        self.add_event(wo_id, "precondition_added", {"ref": ref, "note": note})
        return dict(self.conn.execute(
            "SELECT * FROM preconditions WHERE wo_id=? AND ref=?",
            (wo_id, ref)).fetchone())

    def open_preconditions(self, wo_id: str) -> list[dict[str, Any]]:
        """Every hold still standing on this order, oldest first — `idx_preconditions_open`"""
        rows = self.conn.execute(
            "SELECT * FROM preconditions WHERE wo_id=? AND status='open' "
            "ORDER BY created_at, id", (wo_id,)).fetchall()
        return db.rows_to_dicts(rows)

    def preconditions_of(self, wo_id: str) -> list[dict[str, Any]]:
        """EVERY hold this order has ever carried, oldest first"""
        rows = self.conn.execute(
            "SELECT * FROM preconditions WHERE wo_id=? "
            "ORDER BY created_at, id", (wo_id,)).fetchall()
        return db.rows_to_dicts(rows)

    def clear_precondition(self, id_or_ref: int | str, *, by: str) -> None:
        """Clear one row by its `id` or by its `ref`, naming the party that did it"""
        assert by in PRECONDITION_CLEARED_BY, by
        by_id = isinstance(id_or_ref, int)
        # Column names spelled as literals, never interpolated
        select = ("SELECT wo_id, ref, note FROM preconditions "
                  "WHERE id=? AND status='open'" if by_id else
                  "SELECT wo_id, ref, note FROM preconditions "
                  "WHERE ref=? AND status='open'")
        update = ("UPDATE preconditions SET status='cleared', cleared_at=?, cleared_by=? "
                  "WHERE id=? AND status='open'" if by_id else
                  "UPDATE preconditions SET status='cleared', cleared_at=?, cleared_by=? "
                  "WHERE ref=? AND status='open'")
        rows = self.conn.execute(select, (id_or_ref,)).fetchall()
        self.conn.execute(update, (db.now(), by, id_or_ref))
        for row in rows:
            self.add_event(row["wo_id"], "precondition_cleared",
                           {"ref": row["ref"], "by": by, "note": row["note"]})

    def clear_preconditions(self, wo_id: str, refs: Collection[str], *,
                            by: str) -> list[str]:
        """Clear several of ONE order's holds, by row id — so a ref another order also waits on is untouched"""
        assert by in PRECONDITION_CLEARED_BY, by
        wanted = set(refs)
        rows = [r for r in self.open_preconditions(wo_id) if r["ref"] in wanted]
        for row in rows:
            self.clear_precondition(int(row["id"]), by=by)
        return [row["ref"] for row in rows]

    def get_work_order(self, wo_id: str) -> dict[str, Any]:
        row = self.conn.execute("SELECT * FROM work_orders WHERE id=?", (wo_id,)).fetchone()
        if row is None:
            raise KeyError(f"work order {wo_id!r} not found in {self.db_path}")
        return dict(row)

    def work_orders_for_issue(self, issue_url: str) -> list[dict[str, Any]]:
        """Every work order filed against this tracker issue, newest first (#240)"""
        rows = self.conn.execute(
            "SELECT * FROM work_orders WHERE issue_url=? ORDER BY created_at DESC",
            (issue_url,)).fetchall()
        return db.rows_to_dicts(rows)

    def work_orders_for_pr_number(self, number: int,
                                  limit: int = 20) -> list[dict[str, Any]]:
        """Every work order whose pull-request URL ends `/pull/<number>`, newest first (docs/superpowers/specs/2026-09-26-a-red-default-branch-raises-itself.md §2)"""
        rows = self.conn.execute(
            "SELECT * FROM work_orders WHERE pr_url LIKE ? ORDER BY created_at DESC "
            "LIMIT ?", (f"%/pull/{int(number)}", limit)).fetchall()
        return db.rows_to_dicts(rows)

    # -- the issue <-> work order relation

    def link_issue(self, issue_url: str, unit_id: str, kind: str, *,
                   round_no: int = 0, seat: str = "", announced: bool = False) -> bool:
        """Record that `unit_id` raised, was assigned, or cites `issue_url`"""
        if kind not in ISSUE_LINK_KINDS:
            raise ValueError(f"{kind!r} is not one of {ISSUE_LINK_KINDS}")
        row = self.conn.execute(
            "SELECT kind FROM issue_links WHERE issue_url=? AND unit_id=?",
            (issue_url, unit_id)).fetchone()
        with db.write_transaction(self.conn):
            if row is None:
                self.conn.execute(
                    "INSERT INTO issue_links (issue_url, unit_id, kind, round, seat, "
                    "created_at, announced_at) VALUES (?,?,?,?,?,?,?)",
                    (issue_url, unit_id, kind, round_no, seat, db.now(),
                     db.now() if announced else None))
                return True
            if ISSUE_LINK_KINDS.index(kind) > ISSUE_LINK_KINDS.index(row["kind"]):
                self.conn.execute(
                    "UPDATE issue_links SET kind=?, round=?, seat=? "
                    "WHERE issue_url=? AND unit_id=?",
                    (kind, round_no, seat, issue_url, unit_id))
        return False

    def record_issue(self, issue_url: str, *, number: int = 0, repo: str = "",
                     title: str = "", state: str | None = None) -> None:
        """Remember what the OS knows about one issue"""
        with db.write_transaction(self.conn):
            self.conn.execute(
                "INSERT INTO tracked_issues (issue_url, number, repo, title, state, "
                "checked_at) VALUES (?,?,?,?,?,?) ON CONFLICT(issue_url) DO NOTHING",
                (issue_url, number, repo, title, state or "",
                 db.now() if state else None))
            if number:
                self.conn.execute("UPDATE tracked_issues SET number=? WHERE issue_url=?",
                                  (number, issue_url))
            if repo:
                self.conn.execute("UPDATE tracked_issues SET repo=? WHERE issue_url=?",
                                  (repo, issue_url))
            if title:
                self.conn.execute("UPDATE tracked_issues SET title=? WHERE issue_url=?",
                                  (title, issue_url))
            if state:
                self.conn.execute(
                    "UPDATE tracked_issues SET state=?, checked_at=? WHERE issue_url=?",
                    (state, db.now(), issue_url))

    def record_internal_follow_up(self, digest: str, unit_id: str, *, round: int = 0,
                                  seat: str = "", title: str = "", detail: str = "",
                                  file: str = "", symbol: str = "",
                                  failure: str = "") -> bool:
        """Keep a follow-up whose text may not be published"""
        with db.write_transaction(self.conn):
            cur = self.conn.execute(
                "INSERT OR IGNORE INTO internal_follow_ups "
                "(digest, unit_id, round, seat, title, detail, file, symbol, failure, "
                " created_at) VALUES (?,?,?,?,?,?,?,?,?,?)",
                (digest, unit_id, round, seat, title, detail, file, symbol, failure,
                 db.now()))
            return cur.rowcount > 0

    def internal_follow_ups(self, unit_id: str | None = None) -> list[dict[str, Any]]:
        """Withheld follow-ups — one unit's, or the whole project's, oldest first"""
        sql = "SELECT * FROM internal_follow_ups"
        args: tuple[Any, ...] = ()
        if unit_id:
            sql += " WHERE unit_id=?"
            args = (unit_id,)
        return db.rows_to_dicts(self.conn.execute(sql + " ORDER BY id", args).fetchall())

    def record_issue_label(self, issue_url: str, count: int) -> None:
        """The `referenced: N` the OS last managed to put on the issue"""
        with db.write_transaction(self.conn):
            self.conn.execute(
                "UPDATE tracked_issues SET refs_labelled=? WHERE issue_url=?",
                (count, issue_url))

    def mark_issue_announced(self, issue_url: str, unit_id: str) -> None:
        with db.write_transaction(self.conn):
            self.conn.execute(
                "UPDATE issue_links SET announced_at=? WHERE issue_url=? AND unit_id=?",
                (db.now(), issue_url, unit_id))

    def issue_links_of(self, unit_id: str) -> list[dict[str, Any]]:
        """Every issue this one work order or feature order is linked to"""
        rows = self.conn.execute(
            "SELECT l.*, i.number, i.repo, i.title, i.state, i.checked_at "
            "FROM issue_links l LEFT JOIN tracked_issues i USING (issue_url) "
            "WHERE l.unit_id=? ORDER BY l.round, i.number", (unit_id,)).fetchall()
        return db.rows_to_dicts(rows)

    def issue_raiser(self, issue_url: str) -> dict[str, Any] | None:
        """The `raised` link of this issue — the unit, and panel seat, that filed it"""
        row = self.conn.execute(
            "SELECT * FROM issue_links WHERE issue_url=? AND kind='raised' "
            "ORDER BY created_at LIMIT 1", (issue_url,)).fetchone()
        return dict(row) if row else None

    def issue_board(self) -> list[dict[str, Any]]:
        """Every tracked issue with its reference count, most-referenced first"""
        rows = self.conn.execute(
            "SELECT i.*, COUNT(l.unit_id) AS refs FROM tracked_issues i "
            "JOIN issue_links l USING (issue_url) GROUP BY i.issue_url "
            "ORDER BY refs DESC, i.number DESC").fetchall()
        out = db.rows_to_dicts(rows)
        for row in out:
            row["units"] = [dict(r) for r in self.conn.execute(
                "SELECT unit_id, kind, round, seat FROM issue_links WHERE issue_url=? "
                "ORDER BY created_at", (row["issue_url"],)).fetchall()]
        return out

    def issues_to_sync(self) -> list[dict[str, Any]]:
        """Every linked issue, with its reference count and what is still unsaid"""
        rows = self.conn.execute(
            "SELECT i.*, COUNT(l.unit_id) AS refs, "
            "SUM(CASE WHEN l.announced_at IS NULL THEN 1 ELSE 0 END) AS unannounced "
            "FROM tracked_issues i JOIN issue_links l USING (issue_url) "
            "GROUP BY i.issue_url ORDER BY i.checked_at IS NOT NULL, i.checked_at"
        ).fetchall()
        return db.rows_to_dicts(rows)

    def unannounced_links(self, issue_url: str) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM issue_links WHERE issue_url=? AND announced_at IS NULL "
            "ORDER BY created_at", (issue_url,)).fetchall()
        return db.rows_to_dicts(rows)

    def work_orders_tracking_issues(self) -> list[dict[str, Any]]:
        """Every work order that owns a tracker issue"""
        rows = self.conn.execute(
            "SELECT * FROM work_orders WHERE issue_url IS NOT NULL AND issue_url != ''"
        ).fetchall()
        return db.rows_to_dicts(rows)

    def find_by_session(self, session_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM work_orders WHERE session_id=?", (session_id,)
        ).fetchone()
        return dict(row) if row else None

    def settled_worktrees(self, statuses: tuple[str, ...], settled_before: float,
                          limit: int = MAX_PER_SWEEP_ROWS) -> list[dict[str, Any]]:
        """Settled orders still holding an unreclaimed checkout, oldest settlement first (docs/superpowers/specs/2026-10-22-a-settled-checkout-is-reclaimed.md)"""
        rows = self.conn.execute(
            f"""SELECT * FROM work_orders
                 WHERE status IN ({','.join('?' for _ in statuses)})
                   AND worktree IS NOT NULL AND worktree != ''
                   AND updated_at < ?
                   AND NOT EXISTS (SELECT 1 FROM wo_events e
                                   WHERE e.wo_id = work_orders.id AND e.kind = ?)
                 ORDER BY updated_at LIMIT ?""",
            (*statuses, settled_before, WORKTREE_RECLAIMED_EVENT, limit),
        ).fetchall()
        return db.rows_to_dicts(rows)

    def list_work_orders(
        self, statuses: tuple[str, ...] | None = None, limit: int = 200,
        include_hidden: bool = False,
        active_between: tuple[float, float] | None = None,
    ) -> list[dict[str, Any]]:
        """Work orders, newest first (docs/superpowers/specs/2026-10-07-cost-window-selector.md)"""
        conds, params = [], []
        if statuses:
            conds.append(f"status IN ({','.join('?' for _ in statuses)})")
            params.extend(statuses)
        if not include_hidden:
            conds.append("hidden=0")
        if active_between is not None:
            conds.append("""EXISTS (SELECT 1 FROM wo_turns t WHERE t.wo_id =
                                    work_orders.id AND t.started_at >= ?
                                    AND t.started_at < ?)""")
            params.extend(active_between)
        where = f" WHERE {' AND '.join(conds)}" if conds else ""
        rows = self.conn.execute(
            f"SELECT * FROM work_orders{where} ORDER BY created_at DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        return db.rows_to_dicts(rows)

    def search_work_orders(self, words: Sequence[str],
                           limit: int = 50) -> list[dict[str, Any]]:
        """Work orders matching `words`, most relevant first — SETTLED ONES INCLUDED"""
        expr, params = db.score_sql(words, {
            "id": 3, "title": 3, "description": 1, "result_summary": 1,
            "attention_reason": 1, "branch": 1,
        })
        rows = self.conn.execute(
            f"SELECT *, {expr} AS _score FROM work_orders "
            "WHERE _score > 0 ORDER BY _score DESC, created_at DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        return db.rows_to_dicts(rows)

    def search_feature_orders(self, words: Sequence[str], limit: int = 50,
                              kind: str | None = None) -> list[dict[str, Any]]:
        weights = {"id": 3, "title": 3, "description": 1, "attention_reason": 1}
        if kind == "decision":
            # The report's answer is what a decision is found again by (decision-orders spec §7)
            for field in ("$.summary", "$.recommendation.why"):
                weights[f"(CASE WHEN json_valid(plan) THEN "
                        f"json_extract(plan, '{field}') END)"] = 1
        expr, params = db.score_sql(words, weights)
        where, extra = ("AND kind = ? ", (kind,)) if kind else ("", ())
        rows = self.conn.execute(
            f"SELECT *, {expr} AS _score FROM feature_orders "
            f"WHERE _score > 0 {where}ORDER BY _score DESC, created_at DESC LIMIT ?",
            (*params, *extra, limit),
        ).fetchall()
        return db.rows_to_dicts(rows)

    def search_alarms(self, words: Sequence[str],
                      limit: int = 50) -> list[dict[str, Any]]:
        """Alarms carry their work order's title"""
        expr, params = db.score_sql(words, {
            "a.id": 3, "a.kind": 2, "a.reason": 1, "a.note": 1, "a.verdict_reason": 1,
            "w.title": 1,
        })
        rows = self.conn.execute(
            f"SELECT a.*, w.title AS wo_title, {expr} AS _score FROM wo_alarms a "
            "JOIN work_orders w ON w.id = a.wo_id "
            "WHERE _score > 0 ORDER BY _score DESC, a.ts DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        return db.rows_to_dicts(rows)

    def search_gates(self, words: Sequence[str],
                     limit: int = 50) -> list[dict[str, Any]]:
        expr, params = db.score_sql(words, {
            "a.command": 3, "a.kind": 2, "a.wo_id": 2, "a.justification": 1,
            "a.evidence": 1, "a.decision_reason": 1, "w.title": 1,
        })
        rows = self.conn.execute(
            f"SELECT a.*, w.title AS wo_title, {expr} AS _score FROM approvals a "
            "JOIN work_orders w ON w.id = a.wo_id "
            "WHERE _score > 0 ORDER BY _score DESC, a.ts DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        return db.rows_to_dicts(rows)

    def status_counts(self, include_hidden: bool = False) -> dict[str, int]:
        """How many work orders sit in each status"""
        where = "" if include_hidden else " WHERE hidden=0"
        rows = self.conn.execute(
            f"SELECT status, COUNT(*) AS n FROM work_orders{where} GROUP BY status"
        ).fetchall()
        return {row["status"]: int(row["n"]) for row in rows}

    def dependencies(self, wo: dict[str, Any]) -> list[str]:
        """The ids this work order waits on"""
        return db.from_json(wo.get("depends_on"), []) or []

    def unfinished_dependencies(self, wo_id: str) -> list[dict[str, Any]]:
        """The dependencies still standing between this work order and dispatch"""
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
        """Cut these edges"""
        remaining = [d for d in self.dependencies(self.get_work_order(wo_id))
                     if d not in dep_ids]
        self.update_work_order(wo_id, depends_on=db.to_json(remaining))
        self.add_event(wo_id, "dependencies_dropped",
                       {"dropped": dep_ids, "remaining": remaining})
        return remaining

    def release_dispatch_claim(self, wo_id: str, error: str,
                               max_attempts: int = MAX_DISPATCH_ATTEMPTS) -> str:
        """Hand a claimed work order back after a turn that could never be launched (docs/superpowers/specs/2026-09-18-a-failure-is-not-an-answer.md §5.)"""
        row = self.conn.execute(
            "SELECT dispatch_attempts FROM work_orders WHERE id=?", (wo_id,)).fetchone()
        if row is None:
            return "failed"
        attempts = int(row["dispatch_attempts"] or 0) + 1
        # Through `set_status` rather than raw SQL: no status write without a span (docs/superpowers/specs/2026-09-28-stale-blockers-outlive-what-settled-them.md)
        if attempts >= max_attempts:
            self.set_status(wo_id, "failed", trigger="dispatch_attempts_spent",
                            dispatch_attempts=attempts, retry_after=None)
            return "failed"
        delay = DISPATCH_RETRY_BACKOFF_SECONDS[
            min(attempts - 1, len(DISPATCH_RETRY_BACKOFF_SECONDS) - 1)]
        self.set_status(wo_id, "pending", trigger="dispatch_retry",
                        dispatch_attempts=attempts, retry_after=db.now() + delay)
        return "pending"

    def hold_dispatch(self, wo_id: str, until: float | None) -> None:
        """Defer, or stop deferring, a pending order's dispatch (docs/superpowers/specs/2026-09-27-an-expedited-order-that-lands-ships-a-release.md §5)"""
        self.conn.execute(
            "UPDATE work_orders SET retry_after=?, updated_at=? WHERE id=?",
            (until, db.now(), wo_id))

    def clear_dispatch_attempts(self, wo_id: str) -> None:
        """A launch that worked spends the record of the ones that did not"""
        self.conn.execute(
            "UPDATE work_orders SET dispatch_attempts=0, retry_after=NULL WHERE id=?",
            (wo_id,))

    def plan_hold(self, wo: dict[str, Any]) -> dict[str, Any] | None:
        """`{"fo_id", "planner_id", "n"}` when this child's plan is unratified, else None (docs/superpowers/specs/2026-09-24-a-planner-assumption-holds-its-feature.md §2.1.)"""
        parent = wo.get("parent_id")
        if not parent or wo.get("kind") != "worker":
            return None
        row = self.conn.execute(
            """SELECT f.plan_wo_id planner, COUNT(a.id) n
               FROM feature_orders f JOIN assumptions a ON a.wo_id = f.plan_wo_id
               WHERE f.id = ? AND a.status = 'pending'""", (parent,)).fetchone()
        # An aggregate over no rows still returns one, with a NULL planner and n=0
        if row is None or not row["n"]:
            return None
        return {"fo_id": parent, "planner_id": row["planner"], "n": int(row["n"])}

    def spec_hold(self, wo: dict[str, Any]) -> dict[str, Any] | None:
        """`{"fo_id", "planner_id", "pr_url"}` while this child's spec PR is unmerged, else None (docs/superpowers/specs/2026-10-29-a-feature-spec-pull-request-lands-first.md §2)"""
        parent = wo.get("parent_id")
        if not parent or wo.get("kind") != "worker":
            return None
        row = self.conn.execute(
            f"""SELECT p.id planner, p.pr_url pr_url
               FROM feature_orders f JOIN work_orders p ON p.id = f.plan_wo_id
               WHERE f.id = ? AND COALESCE(p.pr_url, '') != ''
                 AND p.status IN ({",".join("?" for _ in SPEC_PR_OPEN_STATUSES)})""",
            (parent, *SPEC_PR_OPEN_STATUSES)).fetchone()
        if row is None:
            return None
        return {"fo_id": parent, "planner_id": row["planner"], "pr_url": row["pr_url"]}

    def claim_next_pending(self, only: Collection[str] | None = None
                           ) -> dict[str, Any] | None:
        """Atomically claim the oldest claimable pending order (pending -> dispatching) (#843)"""
        where = " AND ".join(rule.sql for rule in CLAIM_RULES)
        only_ids: tuple[str, ...] = ()
        only_clause = ""
        if only is not None:
            only_ids = tuple(only)
            if not only_ids:
                return None
            only_clause = f"AND w.id IN ({','.join('?' for _ in only_ids)})"
        cur = self.conn.execute(
            f"""UPDATE work_orders SET status='dispatching', updated_at=?
               WHERE id = (SELECT w.id FROM work_orders w
                           WHERE {where}
                             {only_clause}
                           ORDER BY w.created_at LIMIT 1)
               RETURNING *"""
            , (db.now(), *_claim_params(), *only_ids),
        )
        row = cur.fetchone()
        if row is not None and cur.rowcount == 1:
            # The claim stays ONE statement; the span is written beside it in the same transaction (docs/superpowers/specs/2026-09-28-stale-blockers-outlive-what-settled-them.md)
            at = db.now()
            self._record_span(row["id"], "wo", "pending", "dispatching", trigger="claim",
                              ts=at)
            # ...and its timeline line, at the same moment as `set_status` writes both
            self.add_event(row["id"], "status", {"status": "dispatching"}, ts=at)
        return dict(row) if row else None

    def claim_refusal(self, wo: dict[str, Any]) -> _ClaimRule | None:
        """The first `CLAIM_RULES` entry that refuses this row, or None"""
        cols = ", ".join(f"({rule.sql}) AS {rule.code}" for rule in CLAIM_RULES)
        row = self.conn.execute(
            f"SELECT {cols} FROM work_orders w WHERE w.id = ?",
            (*_claim_params(), wo["id"]),
        ).fetchone()
        if row is None:
            # No row to find IS the selection rule refusing: `None` here would read as claimable and give a...
            return CLAIM_RULES[0]
        return next((rule for rule in CLAIM_RULES if not row[rule.code]), None)

    def why_not_claimable(self, wo: dict[str, Any]) -> str | None:
        """`claim_refusal`'s sentence, or None when the claim SQL would take this row"""
        refusal = self.claim_refusal(wo)
        return refusal.why if refusal else None

    def count_active(self) -> int:
        """How many work orders are spending one of this project's `max_concurrent` slots (#134)"""
        marks = ",".join("?" for _ in SLOT_STATUSES)
        row = self.conn.execute(
            f"SELECT COUNT(*) c FROM work_orders "
            f"WHERE status IN ({marks}) AND kind != 'manager'",
            SLOT_STATUSES,
        ).fetchone()
        return row["c"]

    def count_active_children(self, fo_id: str) -> int:
        """How many of this feature order's children are occupying a slot right now"""
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

    def adopt_spec_section(self, wo_id: str, section: str) -> bool:
        """Record a spec section on a row that has none (docs/superpowers/specs/2026-10-13-an-assumption-review-payload-carries-its-spec-section.md)"""
        cur = self.conn.execute(
            "UPDATE work_orders SET spec_section=?, updated_at=? "
            "WHERE id=? AND (spec_section IS NULL OR spec_section='')",
            (section, db.now(), wo_id),
        )
        return cur.rowcount > 0

    def set_status(self, wo_id: str, status: str, *, trigger: str = "",
                   **extra: Any) -> None:
        """Move a work order, and record the span (docs/superpowers/specs/2026-09-27-time-in-each-state.md)"""
        assert status in WO_STATUSES, status
        # ONE TRANSACTION for all three rows: a reader between them saw a span with no event (#1205)
        with db.write_transaction(self.conn):
            was = self._status_of("work_orders", wo_id)
            self.update_work_order(wo_id, status=status, **extra)
            if was == status:
                return
            # ONE MOMENT for both rows: the span and the event are one transition
            at = db.now()
            self._record_span(wo_id, "wo", was, status, trigger, ts=at)
            self.add_event(wo_id, "status", {"status": status}, ts=at)
            # THE CHOKEPOINT, ENFORCED (docs/superpowers/specs/2026-09-28-stale-blockers-outlive-what-settled-them.md)
            tail = self.conn.execute(
                "SELECT to_status FROM wo_state_spans WHERE order_id=? AND "
                "order_kind='wo' ORDER BY ts DESC, id DESC LIMIT 1", (wo_id,)).fetchone()
            assert tail is not None and tail["to_status"] == status, (
                f"{wo_id} moved to {status!r} without a span beside it")

    def _status_of(self, table: str, order_id: str) -> str:
        """The status an order is in right now, '' when the row is not there"""
        row = self.conn.execute(
            f"SELECT status FROM {table} WHERE id=?", (order_id,)).fetchone()
        return str(row["status"] or "") if row else ""

    def _record_span(self, order_id: str, kind: str, from_status: str, to_status: str,
                     trigger: str = "", *, ts: float | None = None,
                     approximate: bool = False) -> None:
        """One status transition, stamped now"""
        self.conn.execute(
            """INSERT INTO wo_state_spans (order_id, order_kind, from_status, to_status,
                   ts, trigger, approximate)
               VALUES (?,?,?,?,?,?,?)""",
            (order_id, kind, from_status, to_status, db.now() if ts is None else ts,
             trigger, int(approximate)),
        )

    def state_spans(self, order_id: str) -> list[dict[str, Any]]:
        """Every status transition recorded for one order, oldest first"""
        rows = self.conn.execute(
            "SELECT * FROM wo_state_spans WHERE order_id=? ORDER BY ts, id", (order_id,)
        ).fetchall()
        return db.rows_to_dicts(rows)

    def seal_bill(self, order_id: str, payload_json: str, *, feature: bool = False,
                  at: float | None = None) -> None:
        """Freeze one order's bill"""
        fields = {"bill_json": payload_json, "bill_sealed_at": at or db.now()}
        if feature:
            self.update_feature_order(order_id, **fields)
        else:
            self.update_work_order(order_id, **fields)

    def unsealed_terminal_orders(self, limit: int = 5) -> list[dict[str, Any]]:
        """Settled orders with no bill on record yet — oldest first, so a backlog drains"""
        marks = ", ".join("?" for _ in TERMINAL_STATUSES)
        rows = self.conn.execute(
            f"SELECT * FROM work_orders WHERE status IN ({marks}) AND bill_json IS NULL"
            " ORDER BY updated_at LIMIT ?", (*TERMINAL_STATUSES, limit)).fetchall()
        return [dict(r) for r in rows]

    def resettled_sealed_orders(self, *, feature: bool = False,
                                limit: int = 2) -> list[dict[str, Any]]:
        """Settled orders whose bill was sealed BEFORE their last settlement — reopened since (#870)"""
        table, kind = ("feature_orders", "fo") if feature else ("work_orders", "wo")
        terminal = FO_TERMINAL_STATUSES if feature else TERMINAL_STATUSES
        marks = ", ".join("?" for _ in terminal)
        rows = self.conn.execute(
            f"SELECT o.* FROM {table} o WHERE o.status IN ({marks})"
            " AND o.bill_json IS NOT NULL AND o.bill_sealed_at < ("
            "   SELECT MAX(s.ts) FROM wo_state_spans s WHERE s.order_id=o.id"
            f"  AND s.order_kind=? AND s.to_status IN ({marks}))"
            " ORDER BY o.updated_at LIMIT ?",
            (*terminal, kind, *terminal, limit)).fetchall()
        return [dict(r) for r in rows]

    def settled_release_orders(self, since: float,
                               limit: int = 20) -> list[dict[str, Any]]:
        """Settled orders carrying a release batch, newest settlement first (#945)"""
        rows = self.conn.execute(
            "SELECT * FROM work_orders WHERE status IN ('completed', 'failed')"
            " AND updated_at >= ?"
            " AND CASE WHEN json_valid(metadata)"
            "     THEN json_extract(metadata, '$.release_for_issues') IS NOT NULL"
            "     ELSE 0 END"
            " ORDER BY updated_at DESC LIMIT ?", (since, limit)).fetchall()
        return [dict(r) for r in rows]

    def seal_autopsy(self, order_id: str, payload_json: str, *,
                     at: float | None = None) -> None:
        """Freeze one work order's autopsy"""
        self.update_work_order(order_id, autopsy_json=payload_json,
                               autopsy_sealed_at=at or db.now())

    def unsealed_autopsy_orders(self, limit: int = 5) -> list[dict[str, Any]]:
        """Settled orders with no autopsy on record yet — oldest first, so a backlog drains (docs/superpowers/specs/2026-09-27-order-autopsy-durability.md)"""
        marks = ", ".join("?" for _ in TERMINAL_STATUSES)
        rows = self.conn.execute(
            f"SELECT * FROM work_orders WHERE status IN ({marks}) AND autopsy_json IS"
            " NULL ORDER BY updated_at LIMIT ?", (*TERMINAL_STATUSES, limit)).fetchall()
        return [dict(r) for r in rows]

    def stale_autopsy_orders(self, version: int,
                             limit: int = 5) -> list[dict[str, Any]]:
        """Settled orders whose sealed autopsy predates `version` — oldest first, bounded (docs/superpowers/specs/2026-09-27-order-autopsy-durability.md)"""
        marks = ", ".join("?" for _ in TERMINAL_STATUSES)
        rows = self.conn.execute(
            f"SELECT * FROM work_orders WHERE status IN ({marks})"
            " AND autopsy_json IS NOT NULL"
            " AND CASE WHEN json_valid(autopsy_json)"
            "     THEN COALESCE(json_extract(autopsy_json, '$.payload_v'), 1) < ?"
            "     ELSE 0 END"
            " ORDER BY updated_at LIMIT ?",
            (*TERMINAL_STATUSES, version, limit)).fetchall()
        return [dict(r) for r in rows]

    def sealed_bills_since(self, since: float) -> list[dict[str, Any]]:
        """Every work order whose bill was frozen at or after `since`, newest first (docs/superpowers/findings/2026-08-30-where-the-800-dollars-went.md)"""
        rows = self.conn.execute(
            "SELECT id, title, status, hidden, bill_sealed_at, bill_json"
            " FROM work_orders WHERE bill_json IS NOT NULL AND bill_sealed_at >= ?"
            " ORDER BY bill_sealed_at DESC", (since,)).fetchall()
        return db.rows_to_dicts(rows)

    def last_alarm_of_kind(self, kind: str) -> dict[str, Any] | None:
        """The newest alarm of one kind in this project, whatever became of it"""
        row = self.conn.execute(
            "SELECT * FROM wo_alarms WHERE kind=? ORDER BY ts DESC LIMIT 1",
            (kind,)).fetchone()
        return dict(row) if row else None

    def latest_settled_order(self) -> dict[str, Any] | None:
        """The most recently settled work order, as a CARRIER and nothing more"""
        row = self.conn.execute(
            f"""SELECT * FROM work_orders
                WHERE status IN ({','.join('?' * len(TERMINAL_STATUSES))})
                ORDER BY updated_at DESC LIMIT 1""",
            TERMINAL_STATUSES).fetchone()
        return dict(row) if row else None

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

    def repair_while(self, order_id: str, status: str, repair: Callable[[], None], *,
                     kind: str = "wo") -> bool:
        """Apply an invariant repair only while the row is STILL in the status the checker derived from (#1205)"""
        table = "work_orders" if kind == "wo" else "feature_orders"
        with db.write_transaction(self.conn):
            # An empty status is a derivation nothing can be guarded against
            if not status or self._status_of(table, order_id) != status:
                return False
            repair()
        return True

    def flag_attention_while(self, wo_id: str, reason: str, status: str) -> bool:
        """Flag only while the order is STILL in the status the caller derived from (#1205)"""
        return self.repair_while(wo_id, status,
                                 lambda: self.flag_attention(wo_id, reason))

    def clear_attention(self, wo_id: str) -> None:
        # The blocker is gone, so any ack against it is spent: if the same reason comes back later it is a...
        self.update_work_order(wo_id, needs_attention=0, attention_reason=None,
                               acknowledged_blockers=None)

    def ack_attention(self, wo_id: str, blockers: list[str], by: str = "you") -> None:
        """Record that the user has seen these blockers, and put the flag down"""
        self.update_work_order(wo_id, needs_attention=0, attention_reason=None,
                               acknowledged_blockers=db.to_json(blockers))
        self.add_event(wo_id, "acknowledged", {"blockers": blockers, "by": by})

    def lower_attention(self, wo_id: str) -> None:
        """Put the flag down, dismissing nothing and forgetting nothing"""
        self.update_work_order(wo_id, needs_attention=0, attention_reason=None)

    def set_hidden(self, wo_id: str, hidden: bool = True) -> None:
        """Hide (or unhide) a work order"""
        self.get_work_order(wo_id)  # KeyError if it doesn't exist
        self.update_work_order(wo_id, hidden=1 if hidden else 0)
        self.add_event(wo_id, "hidden", {"hidden": bool(hidden)})

    def set_observability(self, wo_id: str, level: str | None) -> None:
        """Set this order's debug-collection level (docs/superpowers/specs/2026-09-24-order-observability.md)"""
        from . import observability

        if level is not None and level not in observability.LEVELS:
            raise ValueError(f"observability {level!r} not in "
                             f"{list(observability.LEVELS)}")
        self.get_work_order(wo_id)  # KeyError if it doesn't exist
        self.update_work_order(wo_id, observability=level)

    # -- the scheduler's clock (`scheduled_jobs`)

    def seed_schedule(self, job_id: str, now: float | None = None) -> dict[str, Any]:
        """This job's clock, created on first sight if it has never been seen"""
        now = db.now() if now is None else now
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO scheduled_jobs (job_id, created_at, last_fired_at)"
                " VALUES (?,?,?)", (job_id, now, now))
        row = self.conn.execute("SELECT * FROM scheduled_jobs WHERE job_id=?",
                                (job_id,)).fetchone()
        return dict(row)

    def schedule_state(self, job_id: str) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM scheduled_jobs WHERE job_id=?",
                                (job_id,)).fetchone()
        return dict(row) if row else None

    def list_schedule_states(self) -> list[dict[str, Any]]:
        return [dict(r) for r in self.conn.execute(
            "SELECT * FROM scheduled_jobs ORDER BY job_id")]

    def record_schedule_fire(self, job_id: str, wo_id: str,
                             now: float | None = None) -> None:
        """A job fired: advance its clock to NOW and clear any hold"""
        now = db.now() if now is None else now
        with self.conn:
            self.conn.execute(
                "UPDATE scheduled_jobs SET last_fired_at=?, last_wo_id=?, held_since=NULL,"
                " held_reason=NULL WHERE job_id=?", (now, wo_id, job_id))

    def record_schedule_hold(self, job_id: str, reason: str,
                             now: float | None = None) -> None:
        """A job wanted to fire and could not"""
        now = db.now() if now is None else now
        with self.conn:
            self.conn.execute(
                "UPDATE scheduled_jobs SET held_since=COALESCE(held_since, ?),"
                " held_reason=? WHERE job_id=?", (now, reason, job_id))

    def delete_work_order(self, wo_id: str) -> dict[str, int]:
        """Erase a work order and everything hanging off it"""
        self.get_work_order(wo_id)  # KeyError if it doesn't exist
        deleted: dict[str, int] = {}
        with db.write_transaction(self.conn):
            # Foreign keys are on, so a feature order still pointing at this work order as its planner would...
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
            # `health_reviews` has no foreign key (docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md)
            self.conn.execute(
                "DELETE FROM health_reviews WHERE subject_kind='work_order' "
                "AND subject_id=?", (wo_id,))
            # Same reason, same rule: no foreign key to cascade
            self.conn.execute(
                "DELETE FROM wo_state_spans WHERE order_kind='wo' AND order_id=?",
                (wo_id,))
            self.conn.execute("DELETE FROM work_orders WHERE id=?", (wo_id,))
        return deleted

    # -- feature orders

    def create_feature_order(self, title: str, description: str = "",
                             origin: str = "jarvis", backlog_id: str | None = None,
                             metadata: dict[str, Any] | None = None,
                             fo_id: str | None = None,
                             max_parallel: int | None = None,
                             budget_usd: float | None = None,
                             kind: str = "feature") -> dict[str, Any]:
        assert origin in WO_ORIGINS, origin
        assert kind in FO_KINDS, kind
        assert max_parallel is None or max_parallel >= 1, max_parallel
        # Every kind carries its own prefix so that every id in the OS still says what it names on sight (docs/superpowers/specs/2026-09-27-investigation-orders.md)
        fo_id = fo_id or db.new_id(FO_ID_PREFIXES.get(kind, "fo"))
        ts = db.now()
        self.conn.execute(
            """INSERT INTO feature_orders (id, title, description, status, kind, origin,
                   created_at, updated_at, backlog_id, metadata, max_parallel,
                   budget_usd)
               VALUES (?,?,?,'pending',?,?,?,?,?,?,?,?)""",
            (fo_id, title, description, kind, origin, ts, ts, backlog_id,
             db.to_json(metadata or {}), max_parallel, budget_usd),
        )
        # As above: the span a transition cannot supply
        self._record_span(fo_id, "fo", "", "pending", ts=ts)
        return self.get_feature_order(fo_id)

    def get_feature_order(self, fo_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM feature_orders WHERE id=?", (fo_id,)
        ).fetchone()
        if row is None:
            raise KeyError(f"feature order {fo_id!r} not found in {self.db_path}")
        return dict(row)

    def list_feature_orders(self, statuses: tuple[str, ...] | None = None,
                            limit: int = 200,
                            kind: str | None = "feature") -> list[dict[str, Any]]:
        """`kind` DEFAULTS to 'feature', and that default is the whole point: nothing read this column before..."""
        clauses, params = [], []
        if statuses:
            clauses.append(f"status IN ({','.join('?' for _ in statuses)})")
            params += list(statuses)
        if kind is not None:
            clauses.append("kind=?")
            params.append(kind)
        where = f" WHERE {' AND '.join(clauses)}" if clauses else ""
        rows = self.conn.execute(
            f"SELECT * FROM feature_orders{where} ORDER BY created_at DESC LIMIT ?",
            (*params, limit),
        ).fetchall()
        return db.rows_to_dicts(rows)

    def count_feature_orders(self, kind: str, origin: str, since: float) -> int:
        """How many of one kind and origin were created since `since`"""
        return int(self.conn.execute(
            "SELECT COUNT(*) n FROM feature_orders WHERE kind=? AND origin=? "
            "AND created_at >= ?", (kind, origin, float(since))).fetchone()["n"])

    def feature_status_counts(self, kind: str | None = "feature") -> dict[str, int]:
        """How many feature orders sit in each status"""
        where, params = ("", ()) if kind is None else (" WHERE kind=?", (kind,))
        rows = self.conn.execute(
            f"SELECT status, COUNT(*) AS n FROM feature_orders{where} GROUP BY status",
            params,
        ).fetchall()
        return {row["status"]: int(row["n"]) for row in rows}

    def flagged_feature_orders(self) -> list[dict[str, Any]]:
        """Every FAMILY asking for the user, by its own flag or a child's, whatever its KIND"""
        rows = self.conn.execute(
            """SELECT * FROM feature_orders f WHERE f.needs_attention=1
                   OR EXISTS (SELECT 1 FROM work_orders w WHERE w.parent_id=f.id
                              AND w.needs_attention=1 AND w.hidden=0)
               ORDER BY created_at DESC"""
        ).fetchall()
        return db.rows_to_dicts(rows)

    def update_feature_order(self, fo_id: str, *, decided_by_user: bool = False,
                             **fields: Any) -> None:
        if not fields:
            return
        # The never-auto-complete rule: only the user's `jarvis do done|cancel` settles a decision order (§2)
        if fields.get("status") in FO_TERMINAL_STATUSES and not decided_by_user:
            row = self.conn.execute("SELECT kind FROM feature_orders WHERE id=?",
                                    (fo_id,)).fetchone()
            if row is not None and row["kind"] == "decision":
                raise DecisionOrderGuard(
                    f"refusing to write status {fields['status']!r} on decision order "
                    f"{fo_id}: only the user settles it (jarvis do done|cancel {fo_id})")
        fields["updated_at"] = db.now()
        cols = ", ".join(f"{k}=?" for k in fields)
        self.conn.execute(
            f"UPDATE feature_orders SET {cols} WHERE id=?", (*fields.values(), fo_id)
        )

    def set_feature_status(self, fo_id: str, status: str, *, trigger: str = "",
                           decided_by_user: bool = False, **extra: Any) -> None:
        """Move a feature order, and retire its agent type when it settles (docs/superpowers/specs/2026-08-29-spec-driven-feature-orders.md)"""
        from . import specs

        assert status in FO_STATUSES, status
        was = self._status_of("feature_orders", fo_id)
        self.update_feature_order(fo_id, status=status, decided_by_user=decided_by_user,
                                  **extra)
        if status in FO_TERMINAL_STATUSES:
            specs.remove_agent(self.project_path, fo_id)
        if was == status:
            return
        self._record_span(fo_id, "fo", was, status, trigger)

    def flag_feature_attention(self, fo_id: str, reason: str) -> None:
        self.update_feature_order(fo_id, needs_attention=1, attention_reason=reason)

    def clear_feature_attention(self, fo_id: str) -> None:
        self.update_feature_order(fo_id, needs_attention=0, attention_reason=None)

    def raise_decision(self, do_id: str, while_status: str, reason: str, *, title: str,
                       body: str, delivered: tuple[str, dict[str, Any]] | None = None) -> bool:
        """Flag + warning notification, and on `delivered` the status and the event, as ONE unit (docs/superpowers/specs/2026-10-31-decision-orders.md)"""
        with db.write_transaction(self.conn):
            # Re-read inside the lock: the user's `jarvis do done` may have landed since the caller's read
            if self._status_of("feature_orders", do_id) != while_status:
                return False
            if delivered is not None:
                self.set_feature_status(do_id, "awaiting_decision",
                                        trigger=DECISION_DELIVERED_EVENT)
                self.add_event(delivered[0], DECISION_DELIVERED_EVENT, delivered[1])
            self.flag_feature_attention(do_id, reason)
            # `wo_id` carries the do- id so the sinks' deep link opens its page (spec §7)
            self.add_notification(title=title, body=body, level="warning",
                                  wo_id=do_id, source="decision")
        return True

    def last_decision_delivery(self, researcher_id: str) -> dict[str, Any] | None:
        """The payload of the newest `decision_delivered` event, None before the first"""
        rows = self.events_of_kind(researcher_id, DECISION_DELIVERED_EVENT)
        return db.from_json(rows[-1]["payload"], {}) if rows else None

    def decisions_closed_without_user(self) -> list[dict[str, Any]]:
        """Terminal decision orders the user's verb did not close: neither event nor span trigger (docs/superpowers/specs/2026-10-31-decision-orders.md)"""
        marks = ",".join("?" * len(FO_TERMINAL_STATUSES))
        rows = self.conn.execute(
            f"""SELECT f.* FROM feature_orders f
                WHERE f.kind='decision' AND f.status IN ({marks})
                  AND NOT EXISTS (SELECT 1 FROM wo_state_spans s WHERE s.order_id=f.id
                                  AND s.to_status=f.status AND s.trigger=?)
                  AND NOT EXISTS (SELECT 1 FROM wo_events e WHERE e.wo_id=f.plan_wo_id
                                  AND e.kind=?)""",
            (*FO_TERMINAL_STATUSES, DECISION_CLOSED_EVENT, DECISION_CLOSED_EVENT)).fetchall()
        return db.rows_to_dicts(rows)

    def feature_children(self, fo_id: str, *,
                         kinds: tuple[str, ...] | None) -> list[dict[str, Any]]:
        """The family's child work orders, oldest first: `kinds` REQUIRED, None for all"""
        if kinds is None:
            rows = self.conn.execute(
                "SELECT * FROM work_orders WHERE parent_id=? ORDER BY created_at",
                (fo_id,)).fetchall()
        else:
            marks = ",".join("?" * len(kinds))
            rows = self.conn.execute(
                f"SELECT * FROM work_orders WHERE parent_id=? AND kind IN ({marks}) "
                "ORDER BY created_at", (fo_id, *kinds)).fetchall()
        answered = {s["wo_id"] for s in self.superseded_children(fo_id)}
        return [{**c, "superseded": c["id"] in answered}
                for c in db.rows_to_dicts(rows)]

    def superseded_children(self, fo_id: str) -> list[dict[str, Any]]:
        """Which of this feature's children the user has answered for, and why"""
        row = self.conn.execute(
            "SELECT metadata FROM feature_orders WHERE id=?", (fo_id,)
        ).fetchone()
        meta = db.from_json(row["metadata"], {}) if row else {}
        return list((meta or {}).get(SUPERSEDED_CHILDREN_KEY) or [])

    def supersede_children(self, fo_id: str, wo_ids: Iterable[str],
                           note: str = "") -> list[dict[str, Any]]:
        """Record that the user has answered for these children's failure"""
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
        """The feature order whose plan this Neo question is reviewing, if any"""
        row = self.conn.execute(
            "SELECT * FROM feature_orders WHERE plan_question_id=?", (question_id,)
        ).fetchone()
        return dict(row) if row else None

    def feature_order_for_planner(self, wo_id: str) -> dict[str, Any] | None:
        """The feature order a plan question hangs off"""
        row = self.conn.execute(
            "SELECT * FROM feature_orders WHERE id=? OR plan_wo_id=?", (wo_id, wo_id)
        ).fetchone()
        return dict(row) if row else None

    def create_plan_children(self, fo_id: str,
                             ordered: list[dict[str, Any]],
                             manager: bool = False) -> list[dict[str, Any]]:
        """Turn an approved plan into work orders, in one transaction"""
        by_key: dict[str, str] = {}
        created: list[str] = []
        with db.write_transaction(self.conn):
            for child in ordered:
                wo = self.create_work_order(
                    title=child["title"],
                    description=child["description"],
                    origin="os",
                    depends_on=[by_key[k] for k in child["needs"] if k in by_key],
                    parent_id=fo_id,
                    spec_section=child.get("spec_section"),
                )
                by_key[child["key"]] = wo["id"]
                created.append(wo["id"])
            if manager:
                self.create_manager_order(fo_id)
        return [self.get_work_order(wo_id) for wo_id in created]

    def create_manager_order(self, fo_id: str) -> dict[str, Any]:
        """The feature's project manager order"""
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
            origin="os",
            parent_id=fo_id,
            kind="manager",
        )

    def feature_summary(self, kind: str | None = "feature") -> dict[str, Any]:
        """How many feature orders sit in each status, and how many want the user"""
        where, params = ("", ()) if kind is None else (" WHERE kind=?", (kind,))
        by_status = {
            r["status"]: int(r["n"]) for r in self.conn.execute(
                f"SELECT status, COUNT(*) AS n FROM feature_orders{where} "
                f"GROUP BY status", params
            ).fetchall()
        }
        attention = self.conn.execute(
            "SELECT COUNT(*) c FROM feature_orders WHERE needs_attention=1"
            + (" AND kind=?" if kind is not None else ""), params
        ).fetchone()["c"]
        return {"by_status": by_status, "needs_attention": int(attention)}

    # -- events

    def add_event(self, wo_id: str, kind: str, payload: dict[str, Any] | None = None,
                  *, ts: float | None = None) -> None:
        """One timeline row"""
        self.conn.execute(
            "INSERT INTO wo_events (wo_id, ts, kind, payload) VALUES (?,?,?,?)",
            (wo_id, db.now() if ts is None else ts, kind, db.to_json(payload or {})),
        )

    def events_of_kind(self, wo_id: str, kind: str) -> list[dict[str, Any]]:
        """Every event of ONE kind on this work order, oldest first and UNCAPPED"""
        rows = self.conn.execute(
            "SELECT * FROM wo_events WHERE wo_id=? AND kind=? ORDER BY ts",
            (wo_id, kind)).fetchall()
        return db.rows_to_dicts(rows)

    def merged_prs_since(self, ts: float) -> list[dict[str, Any]]:
        """`pr_merged` events after `ts`, oldest first: {wo_id, ts, pr_url} (Morpheus's merge trigger)"""
        rows = db.rows_to_dicts(self.conn.execute(
            "SELECT wo_id, ts, payload FROM wo_events WHERE kind='pr_merged' AND ts > ? "
            "ORDER BY ts", (ts,)).fetchall())
        return [{"wo_id": r["wo_id"], "ts": r["ts"],
                 "pr_url": str((db.from_json(r["payload"], {}) or {}).get("pr_url") or "")}
                for r in rows]

    def work_orders_with_event(self, kind: str, wo_ids: Sequence[str]) -> set[str]:
        """Which of `wo_ids` carry an event of `kind`"""
        ids = list(wo_ids)
        if not ids:
            return set()
        holes = ",".join("?" * len(ids))
        rows = self.conn.execute(
            f"SELECT DISTINCT wo_id FROM wo_events WHERE kind=? AND wo_id IN ({holes})",
            (kind, *ids)).fetchall()
        return {str(row["wo_id"]) for row in rows}

    def last_event_of_kind(self, wo_id: str, kind: str) -> dict[str, Any] | None:
        """The NEWEST event of one kind on this work order, or None (docs/superpowers/specs/2026-09-25-a-cap-hold-must-say-so.md §1)"""
        row = self.conn.execute(
            "SELECT * FROM wo_events WHERE wo_id=? AND kind=? ORDER BY ts DESC LIMIT 1",
            (wo_id, kind)).fetchone()
        return dict(row) if row is not None else None

    def events_across(self, kind: str, limit: int = 200) -> list[dict[str, Any]]:
        """Every event of ONE kind in the project, NEWEST first, with its work order"""
        rows = self.conn.execute(
            "SELECT e.ts AS ts, e.kind AS kind, e.payload AS payload,"
            " w.id AS wo_id, w.title AS title, w.status AS status,"
            " w.hidden AS hidden, w.needs_attention AS needs_attention,"
            " w.attention_reason AS attention_reason"
            " FROM wo_events e JOIN work_orders w ON w.id = e.wo_id"
            " WHERE e.kind=? ORDER BY e.ts DESC LIMIT ?", (kind, limit)).fetchall()
        return db.rows_to_dicts(rows)

    # -- cost alarms

    def add_alarm(self, wo_id: str, kind: str, seq: int, reason: str,
                  status: str = "raised") -> dict[str, Any]:
        """Record one raised alarm and return it"""
        alarm_id = db.new_id("al")
        self.conn.execute(
            """INSERT INTO wo_alarms (id, wo_id, ts, kind, seq, reason, status)
               VALUES (?,?,?,?,?,?,?)""",
            (alarm_id, wo_id, db.now(), kind, int(seq), reason, status),
        )
        return self.get_alarm(alarm_id)

    def add_finding(self, wo_id: str, *, kind: str, reason: str, seq: int = NO_TURN,
                    source: str = "cost", probe: str | None = None,
                    subject_kind: str = "work_order",
                    fo_id: str | None = None, remedy: str | None = None,
                    remedy_argument: str = "",
                    status: str = "raised") -> dict[str, Any]:
        """Record one finding — the general raise, of which `add_alarm` is the cost case (docs/superpowers/specs/2026-08-31-the-supervisor.md)"""
        assert subject_kind in ALARM_SUBJECTS, subject_kind
        assert source in ALARM_SOURCES, source
        assert status in ALARM_STATUSES, status
        if (subject_kind == "feature_order") != bool(fo_id):
            raise ValueError(
                f"subject_kind={subject_kind!r} and fo_id={fo_id!r} disagree: "
                "a feature_order finding needs an fo_id, and a work_order one has none")
        alarm_id = db.new_id("al")
        self.conn.execute(
            """INSERT INTO wo_alarms (id, wo_id, ts, kind, seq, reason,
                                      subject_kind, fo_id, source, probe,
                                      remedy, remedy_argument, status)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (alarm_id, wo_id, db.now(), kind, int(seq), reason,
             subject_kind, fo_id, source, probe, remedy,
             remedy_argument if remedy else None, status),
        )
        return self.get_alarm(alarm_id)

    def get_alarm(self, alarm_id: str) -> dict[str, Any]:
        row = self.conn.execute(
            "SELECT * FROM wo_alarms WHERE id=?", (alarm_id,)).fetchone()
        if row is None:
            raise KeyError(alarm_id)
        return dict(row)

    def alarm_for_question(self, question_id: int) -> dict[str, Any] | None:
        """The alarm this Neo question was escalated from, if any"""
        row = self.conn.execute(
            "SELECT * FROM wo_alarms WHERE neo_question_id=?", (question_id,)).fetchone()
        return dict(row) if row else None

    def alarm_for_remedy_approval(self, approval_id: int) -> dict[str, Any] | None:
        """The alarm whose proposed remedy this `self_heal` approval authorises, if any"""
        row = self.conn.execute(
            "SELECT * FROM wo_alarms WHERE remedy_approval_id=?",
            (approval_id,)).fetchone()
        return dict(row) if row else None

    def alarms_of(self, wo_id: str) -> list[dict[str, Any]]:
        """Every alarm on one work order, oldest first"""
        return db.rows_to_dicts(self.conn.execute(
            "SELECT * FROM wo_alarms WHERE wo_id=? ORDER BY ts", (wo_id,)).fetchall())

    def alarms_for_feature(self, fo_id: str) -> list[dict[str, Any]]:
        """Every finding ABOUT one feature order, oldest first, whatever carried it"""
        return db.rows_to_dicts(self.conn.execute(
            "SELECT * FROM wo_alarms WHERE fo_id=? ORDER BY ts", (fo_id,)).fetchall())

    def alarms_across(self, limit: int = 200, statuses: tuple[str, ...] | None = None,
                      wo_id: str | None = None, fo_id: str | None = None,
                      sources: tuple[str, ...] | None = None) -> list[dict[str, Any]]:
        """Every alarm in the project, NEWEST first, with its work order"""
        where = ["1=1"]
        args: list[Any] = []
        if statuses:
            where.append(f"a.status IN ({','.join('?' for _ in statuses)})")
            args.extend(statuses)
        if wo_id:
            # Filtered here rather than by the caller after the fact: `limit` is applied by SQLite
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
            " a.remedy AS remedy, a.remedy_argument AS remedy_argument,"
            " a.remedy_approval_id AS remedy_approval_id,"
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
        """Atomically claim the OLDEST alarm still `raised`, or None"""
        now = db.now()
        cur = self.conn.execute(
            """UPDATE wo_alarms SET status='reviewing', claimed_at=?
               WHERE id = (SELECT id FROM wo_alarms WHERE status='raised'
                             AND COALESCE(retry_after, 0) <= ?
                           ORDER BY ts LIMIT 1)
               RETURNING *""",
            (now, now),
        )
        row = cur.fetchone()
        return dict(row) if row else None

    def release_alarm_claim(self, alarm_id: str, reason: str,
                            max_attempts: int = 3) -> str:
        """Hand a claimed alarm back after a review call that never happened"""
        row = self.conn.execute("SELECT attempts FROM wo_alarms WHERE id=?",
                                (alarm_id,)).fetchone()
        if row is None:
            return "failed"
        attempts = int(row["attempts"] or 0)
        if attempts >= max_attempts:
            self.update_alarm(
                alarm_id, status="failed", claimed_at=None, decided_at=db.now(),
                verdict_reason=f"{reason} (after {attempts} retries — nobody has "
                               f"judged this)")
            return "failed"
        self.conn.execute(
            "UPDATE wo_alarms SET status='raised', claimed_at=NULL, "
            "attempts=attempts+1, retry_after=?, verdict_reason=? WHERE id=?",
            (db.now() + ALARM_RETRY_BACKOFF_SECONDS[
                min(attempts, len(ALARM_RETRY_BACKOFF_SECONDS) - 1)],
             reason, alarm_id))
        return "raised"

    def reclaim_stale_alarms(self, older_than: float,
                             max_attempts: int) -> dict[str, list[str]]:
        """Unstick alarms parked in `reviewing` by a drain that never finished"""
        cutoff = db.now() - older_than
        # Give up FIRST, then re-queue — `NeoStore.reclaim_stale`'s ordering and its reason: the other way...
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

    # -- health_reviews: the ledger of LOOKING, not of finding (§4)

    def record_health_review(self, subject_kind: str, subject_id: str, *,
                             fingerprint: str, trigger: str, outcome: str,
                             findings: int = 0, detail: str = "",
                             reopens_at: float = 0.0) -> int:
        """One sweep, whatever it concluded"""
        assert subject_kind in HEALTH_SUBJECTS, subject_kind
        assert outcome in HEALTH_OUTCOMES, outcome
        cur = self.conn.execute(
            """INSERT INTO health_reviews (ts, subject_kind, subject_id, fingerprint,
                                           trigger, outcome, findings, detail,
                                           reopens_at)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (db.now(), subject_kind, subject_id, fingerprint, trigger, outcome,
             int(findings), detail, float(reopens_at)),
        )
        return int(cur.lastrowid or 0)

    def last_health_review(self, subject_kind: str,
                           subject_id: str) -> dict[str, Any] | None:
        """The most recent sweep of one unit, or None if it has never been looked at"""
        row = self.conn.execute(
            """SELECT * FROM health_reviews
                WHERE subject_kind=? AND subject_id=?
                  AND outcome NOT IN ('failed','held')
             ORDER BY ts DESC, id DESC LIMIT 1""",
            (subject_kind, subject_id),
        ).fetchone()
        return dict(row) if row else None

    def last_health_attempt_ts(self, subject_kind: str,
                               subject_id: str) -> float | None:
        """When this unit was last SWEPT, whatever came of it — or None if never (#216)"""
        row = self.conn.execute(
            """SELECT ts FROM health_reviews
                WHERE subject_kind=? AND subject_id=? AND outcome!='held'
             ORDER BY ts DESC, id DESC LIMIT 1""",
            (subject_kind, subject_id),
        ).fetchone()
        return float(row["ts"]) if row else None

    def health_reviews_of(self, subject_kind: str,
                          subject_id: str) -> list[dict[str, Any]]:
        """Every sweep of one unit, oldest first"""
        return db.rows_to_dicts(self.conn.execute(
            """SELECT * FROM health_reviews WHERE subject_kind=? AND subject_id=?
             ORDER BY ts, id""", (subject_kind, subject_id)).fetchall())

    def recent_health_reviews(self, limit: int,
                              include_held: bool = True) -> list[dict[str, Any]]:
        """The project's last `limit` sweeps, newest first, of every unit and outcome"""
        where = "" if include_held else "WHERE outcome!='held' "
        return db.rows_to_dicts(self.conn.execute(
            f"SELECT * FROM health_reviews {where}ORDER BY ts DESC, id DESC LIMIT ?",
            (int(limit),)).fetchall())

    def health_sweep_hold(self) -> tuple[float, str] | None:
        """The project's newest usage-limit hold if it has not expired — (reopens_at, detail)"""
        row = self.conn.execute(
            """SELECT reopens_at, detail FROM health_reviews
                WHERE outcome='held' AND reopens_at > ?
             ORDER BY ts DESC, id DESC LIMIT 1""", (db.now(),)).fetchone()
        return (float(row["reopens_at"]), str(row["detail"] or "")) if row else None

    def probes_reported_at(self, subject_kind: str, subject_id: str,
                           fingerprint: str) -> set[str]:
        """Which probes a sweep has already reported about this unit AT THIS FINGERPRINT"""
        found: set[str] = set()
        for row in self.conn.execute(
                """SELECT detail FROM health_reviews
                    WHERE subject_kind=? AND subject_id=? AND fingerprint=?
                      AND outcome='findings'""",
                (subject_kind, subject_id, fingerprint)).fetchall():
            found.update(p for p in str(row["detail"] or "").split(",") if p)
        return found

    def _this_episode(self, wo_id: str, repair: str, kind: str) -> list[dict[str, Any]]:
        """Events of `kind` since this EPISODE began — the last clear or re-arm (docs/superpowers/specs/2026-08-22-a-work-order-heals-its-own-pull-request.md)"""
        rows = self.events_of_kind(wo_id, kind)
        if not rows:
            return []
        opened = (self.events_of_kind(wo_id, f"pr_{repair}_cleared")
                  + self.events_of_kind(wo_id, f"pr_{repair}_rearmed"))
        since = max((e["ts"] for e in opened), default=0.0)
        return [r for r in rows if r["ts"] > since]

    def pr_repair_nudges(self, wo_id: str, repair: str) -> list[dict[str, Any]]:
        """Every ask of this episode, oldest first — the attempts themselves (#469)"""
        return self._this_episode(wo_id, repair, f"pr_{repair}_nudged")

    def pr_repair_attempts(self, wo_id: str, repair: str) -> int:
        """How many times the OS has asked this worker to fix the SAME thing"""
        return len(self.pr_repair_nudges(wo_id, repair))

    def pr_repair_deferred(self, wo_id: str, repair: str, approval_id: int) -> bool:
        """Has this episode already recorded that THIS gate is holding the repair up?"""
        for event in self._this_episode(wo_id, repair, f"pr_{repair}_deferred"):
            if (db.from_json(event["payload"], {}) or {}).get("approval_id") == approval_id:
                return True
        return False

    def pr_repair_gave_up(self, wo_id: str, repair: str) -> bool:
        """Has the OS already stopped trying on this episode and said so?"""
        return bool(self._this_episode(wo_id, repair, f"pr_{repair}_unresolved"))

    def pr_closure_told(self, wo_id: str) -> bool:
        """Has the user already been told about THIS closure of the pull request? (#224)"""
        closed = self.events_of_kind(wo_id, "pr_closed")
        if not closed:
            return False
        reopened = self.events_of_kind(wo_id, "pr_reopened")
        return not reopened or closed[-1]["ts"] > reopened[-1]["ts"]

    def pr_repair_origin(self, wo_id: str, repairs: tuple[str, ...]) -> str | None:
        """The status the newest OPEN repair episode took this work order away from"""
        newest: dict[str, Any] | None = None
        for repair in repairs:
            rows = self._this_episode(wo_id, repair, f"pr_{repair}_nudged")
            if rows and (newest is None or rows[-1]["ts"] > newest["ts"]):
                newest = rows[-1]
        if newest is None:
            return None
        return (db.from_json(newest["payload"], {}) or {}).get("was") or None


    def work_abandoned(self, wo_id: str) -> bool:
        """Has the worker declared this work deliberately dropped, and not since landed? (#232)"""
        dropped = self.events_of_kind(wo_id, "abandoned")
        if not dropped:
            return False
        delivered = self.events_of_kind(wo_id, "finished")
        return not delivered or float(dropped[-1]["ts"]) >= float(delivered[-1]["ts"])

    def work_unlanded_open(self, wo_id: str, closed_by: str = "") -> bool:
        """Did a landing refuse this work order, with nothing since answering it? (#232)"""
        parked = [e for e in self.events_of_kind(wo_id, "work_unlanded")
                  if not closed_by
                  or (db.from_json(e["payload"], {}) or {}).get("closed_by") == closed_by]
        if not parked:
            return False
        since = float(parked[-1]["ts"])
        return not any(
            float(e["ts"]) > since
            for kind in ("finished", "abandoned", "pr_merged")
            for e in self.events_of_kind(wo_id, kind))

    def release_red_park_open(self, wo_id: str) -> dict[str, Any] | None:
        """The open red-base park on this release order, or None"""
        parked = self.events_of_kind(wo_id, release.RED_PARK_EVENT)
        if not parked:
            return None
        since = float(parked[-1]["ts"])
        closed = any(float(e["ts"]) > since
                     for kind in ("finished", "abandoned", "release_completed")
                     for e in self.events_of_kind(wo_id, kind))
        return None if closed else db.from_json(parked[-1]["payload"], {})

    def pr_pending_open(self, wo_id: str) -> dict[str, Any] | None:
        """The open pushed-branch-no-pull-request park's payload, or None (docs/superpowers/specs/2026-10-12-a-pushed-branch-with-no-pull-request-is-a-state.md §4)"""
        parked = self.events_of_kind(wo_id, PR_PENDING_EVENT)
        if not parked:
            return None
        since = float(parked[-1]["ts"])
        closed = any(float(e["ts"]) > since
                     for kind in ("finished", "abandoned", "pr_url_recorded",
                                  "pr_merged")
                     for e in self.events_of_kind(wo_id, kind))
        return None if closed else db.from_json(parked[-1]["payload"], {})

    def os_prompt_refusal_open(self, wo_id: str) -> dict[str, Any] | None:
        """The open prompt-ceiling refusal on this work order, or None (docs/superpowers/specs/2026-09-26-bounded-model-inputs.md)"""
        refused = self.events_of_kind(wo_id, OS_PROMPT_REFUSED_EVENT)
        if not refused:
            return None
        since = float(refused[-1]["ts"])
        closed = any(float(e["ts"]) > since
                     for kind in OS_PROMPT_REFUSAL_CLEARED_BY
                     for e in self.events_of_kind(wo_id, kind))
        return None if closed else db.from_json(refused[-1]["payload"], {})

    def count_events(self, wo_id: str, exclude: tuple[str, ...] = ()) -> int:
        """How many events this work order has, unbounded, minus the kinds named"""
        holes = ",".join("?" for _ in exclude)
        clause = f" AND kind NOT IN ({holes})" if exclude else ""
        return int(self.conn.execute(
            f"SELECT COUNT(*) FROM wo_events WHERE wo_id=?{clause}",
            (wo_id, *exclude)).fetchone()[0])

    def event_kind_counts(self, wo_id: str) -> dict[str, int]:
        """`{kind: count}` over this work order's whole timeline (docs/superpowers/specs/2026-09-27-self-evolution.md §5)"""
        rows = self.conn.execute(
            "SELECT kind, COUNT(*) AS n FROM wo_events WHERE wo_id=? GROUP BY kind",
            (wo_id,)).fetchall()
        return {str(r["kind"]): int(r["n"]) for r in rows}

    def list_events(self, wo_id: str, limit: int | None = 200, *,
                    newest: bool = True) -> list[dict[str, Any]]:
        """Events oldest first; `limit` keeps the NEWEST that many (io-df4fa1e3), None keeps all"""
        if limit is None:
            rows = self.conn.execute(
                "SELECT * FROM wo_events WHERE wo_id=? ORDER BY ts, id", (wo_id,)).fetchall()
            return db.rows_to_dicts(rows)
        rows = self.conn.execute(
            "SELECT * FROM (SELECT * FROM wo_events WHERE wo_id=? "
            "ORDER BY ts DESC, id DESC LIMIT ?) ORDER BY ts, id", (wo_id, limit)
        ).fetchall()
        return db.rows_to_dicts(rows)

    # -- messages (user feedback queue)

    def queue_message(self, wo_id: str, content: str, source: str = "jarvis",
                      direction: str = "user_to_agent", status: str = "queued",
                      authored_by: str = "") -> int:
        """Put one message on the work order's queue (docs/superpowers/specs/2026-09-11-a-gate-request-carries-the-users-words.md)"""
        cur = self.conn.execute(
            "INSERT INTO wo_messages (wo_id, ts, direction, content, source, status, "
            "authored_by) VALUES (?,?,?,?,?,?,?)",
            (wo_id, db.now(), direction, content, source, status, authored_by),
        )
        if direction == "user_to_agent" and status == "queued":
            flag = inbox_flag(self.project_path, wo_id)
            try:
                flag.parent.mkdir(parents=True, exist_ok=True)
                flag.touch()
            except OSError:
                pass  # the flag only speeds the mid-turn path; the daemon delivers regardless
        return int(cur.lastrowid)

    def claim_message(self, msg_id: int) -> bool:
        """Mark one QUEUED message delivered; False when someone else already took it"""
        cur = self.conn.execute(
            "UPDATE wo_messages SET status='delivered', delivered_at=? "
            "WHERE id=? AND status='queued'", (db.now(), msg_id))
        return cur.rowcount == 1

    def record_agent_reply(self, wo_id: str, content: str, source: str = "worker") -> int:
        """Persist a worker's final assistant message into the work order record"""
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

    def deliverable_messages(self, wo_id: str | None = None) -> list[dict[str, Any]]:
        """`queued_messages` minus the ones held by a delivery backoff"""
        now = db.now()
        return [m for m in self.queued_messages(wo_id)
                if float(m.get("retry_after") or 0.0) <= now]

    def record_delivery_failure(
            self, msg_id: int, error: str,
            max_attempts: int = MAX_MESSAGE_DELIVERY_ATTEMPTS) -> str:
        """One failed attempt to put a queued message into its worker's conversation"""
        row = self.conn.execute("SELECT attempts FROM wo_messages WHERE id=?",
                                (msg_id,)).fetchone()
        if row is None:
            return "failed"
        attempts = int(row["attempts"] or 0) + 1
        if attempts >= max_attempts:
            self.conn.execute(
                "UPDATE wo_messages SET status='failed', attempts=?, last_error=? "
                "WHERE id=?", (attempts, error, msg_id))
            return "failed"
        delay = MESSAGE_RETRY_BACKOFF_SECONDS[
            min(attempts - 1, len(MESSAGE_RETRY_BACKOFF_SECONDS) - 1)]
        self.conn.execute(
            "UPDATE wo_messages SET attempts=?, retry_after=?, last_error=? WHERE id=?",
            (attempts, db.now() + delay, error, msg_id))
        return "queued"

    def list_messages(self, wo_id: str, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM wo_messages WHERE wo_id=? ORDER BY ts LIMIT ?", (wo_id, limit)
        ).fetchall()
        return db.rows_to_dicts(rows)

    def user_messages(self, wo_id: str, limit: int = 100) -> list[dict[str, Any]]:
        """Inbound messages this work order can PROVE the user wrote, newest first"""
        rows = self.conn.execute(
            "SELECT * FROM wo_messages WHERE wo_id=? AND direction='user_to_agent' "
            "AND authored_by=? ORDER BY ts DESC, id DESC LIMIT ?",
            (wo_id, MESSAGE_AUTHOR_USER, limit),
        ).fetchall()
        return db.rows_to_dicts(rows)

    # -- envelopes (the message bus)

    def post_envelope(self, *, from_role: str, to_role: str, kind: str,
                      payload: dict[str, Any] | None = None,
                      subject_wo_id: str | None = None,
                      subject_fo_id: str | None = None) -> int:
        """Queue one envelope"""
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

    def get_envelope(self, env_id: int) -> dict[str, Any] | None:
        """One envelope by id, or None"""
        row = self.conn.execute("SELECT * FROM envelopes WHERE id=?",
                                (env_id,)).fetchone()
        return dict(row) if row else None

    def get_message(self, msg_id: int) -> dict[str, Any] | None:
        """One `wo_messages` row by id, or None"""
        row = self.conn.execute("SELECT * FROM wo_messages WHERE id=?",
                                (msg_id,)).fetchone()
        return dict(row) if row else None

    def envelope_for_message(self, msg_id: int) -> dict[str, Any] | None:
        """The envelope a queued message came from, or None if it came from nobody"""
        row = self.conn.execute("SELECT * FROM envelopes WHERE delivered_msg_id=?",
                                (msg_id,)).fetchone()
        return dict(row) if row else None

    def queued_envelopes(self, limit: int = 200) -> list[dict[str, Any]]:
        """Undelivered envelopes, oldest first"""
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
        """Count one routing attempt, successful or not"""
        self.conn.execute(
            "UPDATE envelopes SET attempts = attempts + 1 WHERE id=?", (env_id,))

    def deliver_envelope(self, env_id: int, wo_id: str, content: str,
                         source: str = "bus", note: str = "") -> int:
        """Hand one envelope to a work order's message queue"""
        with db.write_transaction(self.conn):
            msg_id = self.queue_message(wo_id, content, source=source)
            self.conn.execute(
                "UPDATE envelopes SET state='delivered', delivered_wo_id=?, note=?, "
                "delivered_msg_id=? WHERE id=?", (wo_id, note, msg_id, env_id))
        return msg_id

    def envelopes(self, subject_wo_id: str | None = None,
                  subject_fo_id: str | None = None,
                  limit: int = 200) -> list[dict[str, Any]]:
        """One subject's envelopes, oldest first"""
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
        """The work order that owns this feature's follow-through, whatever its status"""
        row = self.conn.execute(
            "SELECT * FROM work_orders WHERE parent_id=? AND kind='manager' "
            "ORDER BY created_at LIMIT 1", (fo_id,)
        ).fetchone()
        return dict(row) if row else None

    def carrier_for_feature(self, fo_id: str) -> dict[str, Any] | None:
        """The work order that carries a record ABOUT this feature: manager, planner, newest child, or None (docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md)"""
        manager = self.manager_work_order(fo_id)
        if manager:
            return manager
        row = self.conn.execute(
            "SELECT w.* FROM feature_orders f JOIN work_orders w ON w.id = f.plan_wo_id"
            " WHERE f.id=?", (fo_id,)).fetchone()
        if row:
            return dict(row)
        # Newest, not oldest: `feature_children` is ordered by the plan's dependency order
        row = self.conn.execute(
            "SELECT * FROM work_orders WHERE parent_id=? AND kind='worker'"
            " ORDER BY created_at DESC LIMIT 1", (fo_id,)).fetchone()
        return dict(row) if row else None

    # -- turns (the worker's conversation)

    def create_turn(self, wo_id: str, kind: str, prompt: str,
                    msg_id: int | None = None, outfile: str = "",
                    errfile: str = "", cache_ttl: str = "") -> dict[str, Any]:
        """Open a turn row"""
        assert kind in TURN_KINDS, kind
        seq = self.conn.execute(
            "SELECT COALESCE(MAX(seq), 0) + 1 AS n FROM wo_turns WHERE wo_id=?", (wo_id,)
        ).fetchone()["n"]
        cur = self.conn.execute(
            """INSERT INTO wo_turns (wo_id, seq, kind, msg_id, prompt, started_at,
                                     outfile, errfile, cache_ttl)
               VALUES (?,?,?,?,?,?,?,?,?)""",
            (wo_id, seq, kind, msg_id, prompt, db.now(), outfile, errfile, cache_ttl),
        )
        return self.get_turn(int(cur.lastrowid))  # type: ignore[arg-type,return-value]

    def get_turn(self, turn_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM wo_turns WHERE id=?", (turn_id,)).fetchone()
        return dict(row) if row else None

    def set_turn_pid(self, turn_id: int, pid: int | None,
                     unit: str | None = None) -> None:
        """Record how to reach the turn's process"""
        self.conn.execute("UPDATE wo_turns SET pid=?, unit=? WHERE id=?",
                          (pid, unit, turn_id))

    def finish_turn(self, turn_id: int, state: str, result: str | None = None,
                    error: str | None = None, cost_usd: float | None = None,
                    num_turns: int | None = None,
                    usage_json: str | None = None,
                    terminal_reason: str | None = None,
                    api_error_status: int | None = None,
                    cost_source: str | None = None) -> dict[str, Any]:
        """Settle one turn (#471)"""
        assert state in ("done", "failed"), state
        assert cost_source in (None, COST_FROM_ENVELOPE, COST_FROM_TRANSCRIPT), \
            cost_source
        self.conn.execute(
            """UPDATE wo_turns SET state=?, ended_at=?, result=?, error=?, cost_usd=?,
                                   cost_source=?, num_turns=?, usage_json=?,
                                   terminal_reason=?, api_error_status=? WHERE id=?""",
            (state, db.now(), result, error, cost_usd,
             (cost_source or COST_FROM_ENVELOPE) if cost_usd is not None else None,
             num_turns, usage_json, terminal_reason or None, api_error_status, turn_id),
        )
        return self.get_turn(turn_id)  # type: ignore[return-value]

    def set_turn_usage(self, turn_id: int, usage_json: str,
                       cost_usd: float | None = None) -> None:
        """Backfill a settled turn's recorded usage (parsed late from its outfile) (#471)"""
        cost = cost_usd
        if cost is None:
            cost = (db.from_json(usage_json, None) or {}).get("total_cost_usd")
        self.conn.execute(
            "UPDATE wo_turns SET usage_json=?, cost_usd=COALESCE(?, cost_usd), "
            "cost_source=CASE WHEN ? IS NULL THEN cost_source ELSE ? END WHERE id=?",
            (usage_json, cost, cost, COST_FROM_ENVELOPE, turn_id))

    def set_turn_compaction(self, turn_id: int, found: dict[str, Any] | None) -> None:
        """Record what a compaction turn's transcript says it did — `found` None is NO boundary"""
        record = ({"pre": int(found.get("pre") or 0), "post": int(found.get("post") or 0)}
                  if found else {})
        self.conn.execute("UPDATE wo_turns SET compaction_json=? WHERE id=?",
                          (db.to_json(record), turn_id))

    def settle_turn_landed(self, turn_id: int) -> None:
        """A failed turn the record proves did what it was for: `done`, the error kept as history"""
        self.conn.execute("UPDATE wo_turns SET state='done' WHERE id=? AND state='failed'",
                          (turn_id,))

    def compaction_turns_since(self, since: float) -> list[dict[str, Any]]:
        """Every compaction turn started since `since`, with the turn before it, oldest first"""
        rows = self.conn.execute(
            """SELECT t.*, p.kind AS prev_kind, p.state AS prev_state,
                      p.seq AS prev_seq, p.compaction_json AS prev_compaction_json
                 FROM wo_turns t
                 LEFT JOIN wo_turns p ON p.wo_id = t.wo_id AND p.seq = t.seq - 1
                WHERE t.kind = ? AND t.started_at >= ?
                ORDER BY t.started_at""",
            (COMPACT_TURN, since)).fetchall()
        return db.rows_to_dicts(rows)

    def latest_turn(self, wo_id: str) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM wo_turns WHERE wo_id=? ORDER BY seq DESC LIMIT 1", (wo_id,)
        ).fetchone()
        return dict(row) if row else None

    def turn_opened_by(self, turn: dict[str, Any] | None) -> str:
        """Who wrote the message that opened this turn — `wo_messages.source` (#705)"""
        if not turn or turn.get("msg_id") is None:
            return ""
        row = self.conn.execute("SELECT source FROM wo_messages WHERE id=?",
                                (turn["msg_id"],)).fetchone()
        return str(row["source"]) if row else ""

    def turn_usage_before(self, wo_id: str, seq: int) -> dict[str, Any] | None:
        """The usage envelope of the last turn recorded before `seq`, or None"""
        row = self.conn.execute(
            "SELECT usage_json FROM wo_turns WHERE wo_id=? AND seq<? AND "
            "usage_json IS NOT NULL ORDER BY seq DESC LIMIT 1", (wo_id, seq)
        ).fetchone()
        envelope = db.from_json(row["usage_json"], None) if row else None
        return envelope if isinstance(envelope, dict) else None

    def running_turns(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM wo_turns WHERE state='running' ORDER BY started_at"
        ).fetchall()
        return db.rows_to_dicts(rows)

    def closed_background_sessions(self) -> list[dict[str, Any]]:
        """Completed or cancelled orders whose background session has not been removed yet"""
        rows = self.conn.execute(
            "SELECT * FROM work_orders WHERE transport='background' AND bg_id IS NOT NULL "
            "AND status IN ('completed','cancelled') AND id NOT IN "
            "(SELECT wo_id FROM wo_turns WHERE state='running')"
        ).fetchall()
        return db.rows_to_dicts(rows)

    def forget_background_session(self, wo_id: str) -> None:
        """The order's session was removed; `updated_at` stays, a closed order has not moved"""
        self.conn.execute("UPDATE work_orders SET bg_id=NULL WHERE id=?", (wo_id,))

    def count_running_turns(self) -> int:
        """How many of this project's turns are in flight — see src/jarvis/fleet.py"""
        return int(self.conn.execute(
            "SELECT COUNT(*) FROM wo_turns WHERE state='running'").fetchone()[0])

    def list_turns(self, wo_id: str, limit: int = 100) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM wo_turns WHERE wo_id=? ORDER BY seq LIMIT ?", (wo_id, limit)
        ).fetchall()
        return db.rows_to_dicts(rows)

    def all_turns(self, wo_id: str) -> list[dict[str, Any]]:
        """EVERY turn of the conversation, in order and with no ceiling"""
        rows = self.conn.execute(
            "SELECT * FROM wo_turns WHERE wo_id=? ORDER BY seq", (wo_id,)).fetchall()
        return db.rows_to_dicts(rows)

    def turn_starts(self, wo_id: str) -> list[tuple[int, float]]:
        """`(seq, started_at)` for every turn"""
        rows = self.conn.execute(
            "SELECT seq, started_at FROM wo_turns WHERE wo_id=? ORDER BY seq",
            (wo_id,)).fetchall()
        return [(int(r["seq"]), float(r["started_at"])) for r in rows]

    def turn_arms(self, since: float) -> list[tuple[str, float, float, str]]:
        """`(session_id, started_at, ended_at, cache_ttl)` per STAMPED turn since `since` (docs/superpowers/specs/2026-10-15-the-hour-on-worker-turns-only.md)"""
        rows = self.conn.execute(
            """SELECT w.session_id AS session_id, t.started_at AS started_at,
                      t.ended_at AS ended_at, t.cache_ttl AS cache_ttl
                 FROM wo_turns t JOIN work_orders w ON w.id = t.wo_id
                WHERE t.cache_ttl != '' AND w.session_id IS NOT NULL
                      AND t.started_at >= ?""",
            (since,)).fetchall()
        return [(str(r["session_id"]), float(r["started_at"]),
                 float(r["ended_at"]) if r["ended_at"] is not None else float("inf"),
                 str(r["cache_ttl"])) for r in rows]

    def turn_arm_rates(self, since: float) -> list[tuple[str, int, str, float,
                                                          int, int]]:
        """Each STAMPED turn with recorded usage, and the TTL split it paid (issue 1227)"""
        rows = self.conn.execute(
            """SELECT wo_id, seq, cache_ttl, started_at,
                      COALESCE(json_extract(usage_json, '$.cache_1h'), 0) AS cache_1h,
                      COALESCE(json_extract(usage_json, '$.cache_5m'), 0) AS cache_5m
                 FROM wo_turns
                WHERE cache_ttl != '' AND usage_json IS NOT NULL AND started_at >= ?""",
            (since,)).fetchall()
        return [(str(r["wo_id"]), int(r["seq"]), str(r["cache_ttl"]),
                 float(r["started_at"]), int(r["cache_1h"] or 0),
                 int(r["cache_5m"] or 0)) for r in rows]

    def recent_turns(self, wo_id: str, limit: int = 20) -> list[dict[str, Any]]:
        """The conversation's most recent turns, newest first"""
        rows = self.conn.execute(
            "SELECT * FROM wo_turns WHERE wo_id=? ORDER BY seq DESC LIMIT ?",
            (wo_id, limit),
        ).fetchall()
        return db.rows_to_dicts(rows)

    # -- notifications outbox

    def add_notification(self, title: str, body: str = "", level: str = "info",
                         wo_id: str | None = None, source: str = "",
                         paired: bool = False) -> int:
        """`paired`: raised WITH the order's attention flag, so acking either settles both (io-d2a5dd69)"""
        assert level in ("info", "warning", "critical"), level
        reason = None
        if paired and wo_id:
            row = self.conn.execute(
                "SELECT needs_attention, attention_reason FROM work_orders WHERE id=?",
                (wo_id,)).fetchone()
            if row is not None and row["needs_attention"]:
                reason = row["attention_reason"] or ""
        cur = self.conn.execute(
            "INSERT INTO notifications (ts, level, title, body, wo_id, source, paired_reason)"
            " VALUES (?,?,?,?,?,?,?)",
            (db.now(), level, title, body, wo_id, source, reason),
        )
        return int(cur.lastrowid)

    def unrouted_notifications(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM notifications WHERE status='new' ORDER BY ts"
        ).fetchall()
        return db.rows_to_dicts(rows)

    def mark_notification_routed(self, notif_id: int) -> None:
        self.conn.execute("UPDATE notifications SET status='routed' WHERE id=?", (notif_id,))

    def revise_unrouted(self, title: str, wo_id: str | None, *, body: str, level: str) -> int:
        """`CentralStore.revise_inbox` for a notice not routed yet"""
        cur = self.conn.execute(
            "UPDATE notifications SET body=?, level=? WHERE title=? AND wo_id IS ?"
            " AND status='new'", (body, level, title, wo_id or None))
        return cur.rowcount

    # -- invariant violation reports

    def open_violation_report(self, invariant: str, wo_id: str | None = None,
                              level: str = "warning", detail: str = "",
                              owed: str = "") -> bool:
        """Note that this violation is standing"""
        key = (invariant, wo_id or "")
        now = db.now()
        row = self.conn.execute(
            "SELECT seen FROM violation_reports WHERE invariant=? AND wo_id=?", key
        ).fetchone()
        if row is None:
            self.conn.execute(
                "INSERT INTO violation_reports (invariant, wo_id, first_seen, last_seen,"
                " level, detail, owed) VALUES (?,?,?,?,?,?,?)",
                (*key, now, now, level, detail, owed))
            return True
        self.conn.execute(
            "UPDATE violation_reports SET last_seen=?, seen=seen+1, level=?, detail=?,"
            " owed=? WHERE invariant=? AND wo_id=?",
            (now, level, detail, owed, *key))
        return False

    def owed_violations(self, wo_id: str) -> list[dict[str, Any]]:
        """Standing violations on this work order that OWE THE USER A DECISION"""
        rows = self.conn.execute(
            "SELECT * FROM violation_reports WHERE wo_id=? AND owed<>''"
            " ORDER BY first_seen", (wo_id,)).fetchall()
        return db.rows_to_dicts(rows)

    def standing_violations(self, level: str = "") -> list[dict[str, Any]]:
        """Reports still standing, optionally only those at one level"""
        clause = " WHERE level=?" if level else ""
        rows = self.conn.execute(
            f"SELECT * FROM violation_reports{clause} ORDER BY first_seen",
            (level,) if level else ()).fetchall()
        return db.rows_to_dicts(rows)

    def close_violation_reports(
            self, standing: Iterable[tuple[str, str | None]]) -> list[tuple[str, str]]:
        """Forget every report whose violation is not in `standing`"""
        keys = {(inv, wo or "") for inv, wo in standing}
        gone = [(r["invariant"], r["wo_id"])
                for r in self.conn.execute(
                    "SELECT invariant, wo_id FROM violation_reports").fetchall()
                if (r["invariant"], r["wo_id"]) not in keys]
        for key in gone:
            self.conn.execute(
                "DELETE FROM violation_reports WHERE invariant=? AND wo_id=?", key)
        return gone

    def close_violation_report(self, invariant: str, wo_id: str | None = None) -> bool:
        """Forget ONE report, so the same violation coming back is announced again (docs/superpowers/specs/2026-09-26-a-red-default-branch-raises-itself.md §2)"""
        cur = self.conn.execute(
            "DELETE FROM violation_reports WHERE invariant=? AND wo_id=?",
            (invariant, wo_id or ""))
        return cur.rowcount > 0

    def violation_reports(self) -> list[dict[str, Any]]:
        rows = self.conn.execute(
            "SELECT * FROM violation_reports ORDER BY first_seen").fetchall()
        return db.rows_to_dicts(rows)

    # -- assumptions

    def add_assumption(self, wo_id: str, content: str, *, spec_path: str | None = None,
                       spec_which: str | None = None,
                       spec_text: str | None = None) -> int:
        """Record one assumption, with the spec section it cites if it cites one"""
        n = 1 + int(self.conn.execute(
            "SELECT COUNT(*) FROM assumptions WHERE wo_id=?", (wo_id,)
        ).fetchone()[0])
        cur = self.conn.execute(
            "INSERT INTO assumptions (wo_id, ts, content, spec_path, spec_which, "
            "spec_text) VALUES (?,?,?,?,?,?)",
            (wo_id, db.now(), content, spec_path, spec_which, spec_text),
        )
        self.add_event(wo_id, "assumption", {"content": content, "n": n})
        return int(cur.lastrowid)

    def pending_assumptions(self, wo_id: str | None = None) -> list[dict[str, Any]]:
        if wo_id:
            return [a for a in self.all_assumptions(wo_id) if a["status"] == "pending"]
        # Fleet-wide view: assumptions of hidden work orders aren't asking for review
        rows = self.conn.execute(
            """SELECT a.* FROM assumptions a JOIN work_orders w ON w.id = a.wo_id
               WHERE a.status='pending' AND w.hidden=0 ORDER BY a.ts"""
        ).fetchall()
        return db.rows_to_dicts(rows)

    def all_assumptions(self, wo_id: str) -> list[dict[str, Any]]:
        """Every assumption of a work order, reviewed or not, each numbered from 1 (docs/superpowers/specs/2026-08-23-the-work-order-record.md)"""
        rows = self.conn.execute(
            "SELECT * FROM assumptions WHERE wo_id=? ORDER BY ts, id", (wo_id,)
        ).fetchall()
        return [{**a, "n": i} for i, a in enumerate(db.rows_to_dicts(rows), start=1)]

    def review_assumption(self, assumption_id: int, status: str, *,
                          decided_by: str = "", reason: str = "", model: str = "",
                          config_version: str | None = None) -> None:
        """Settle one assumption, recording WHO settled it and on what basis"""
        # `refined`: Neo returned it to the worker to split, per Q2402 (wo-ea3c6f0c)
        assert status in ("accepted", "rejected", "refined"), status
        self.conn.execute(
            """UPDATE assumptions SET status=?, decided_by=?, decided_reason=?,
                   decided_model=?, decided_config_version=? WHERE id=?""",
            (status, decided_by, reason, model, config_version, assumption_id))

    def get_assumption(self, assumption_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM assumptions WHERE id=?",
                                (assumption_id,)).fetchone()
        return dict(row) if row else None

    def link_assumption_question(self, assumption_id: int, question_id: int) -> None:
        self.conn.execute("UPDATE assumptions SET neo_question_id=? WHERE id=?",
                          (question_id, assumption_id))

    def clear_assumption_question(self, assumption_id: int) -> None:
        """Forget an ask the OS itself threw the ruling away on, so the next pass re-asks"""
        self.conn.execute("UPDATE assumptions SET neo_question_id=NULL WHERE id=?",
                          (assumption_id,))

    def assumption_for_question(self, question_id: int) -> dict[str, Any] | None:
        """The assumption this Neo question is ruling on, if any"""
        row = self.conn.execute(
            "SELECT * FROM assumptions WHERE neo_question_id=? OR confirm_question_id=?",
            (question_id, question_id)).fetchone()
        return dict(row) if row else None

    # -- the early (provisional) verdict and the objection

    def record_provisional(self, assumption_id: int, *, verdict: str, reason: str = "",
                           model: str = "", stakes: str = "",
                           config_version: str | None = None) -> None:
        """Stamp the verdict formed while the worker was still typing"""
        assert verdict in PROVISIONAL_VERDICTS, verdict
        self.conn.execute(
            """UPDATE assumptions SET provisional_verdict=?, provisional_reason=?,
                   provisional_model=?, provisional_stakes=?, provisional_ts=?,
                   provisional_config_version=? WHERE id=?""",
            (verdict, reason, model, stakes or "unclassified", db.now(),
             config_version, assumption_id))

    def link_assumption_confirmation(self, assumption_id: int,
                                     question_id: int) -> None:
        """Link the SECOND question"""
        self.conn.execute("UPDATE assumptions SET confirm_question_id=? WHERE id=?",
                          (question_id, assumption_id))

    def clear_assumption_confirmation(self, assumption_id: int) -> None:
        """Forget a confirmation the OS itself threw the ruling away on, so it re-asks"""
        self.conn.execute("UPDATE assumptions SET confirm_question_id=NULL WHERE id=?",
                          (assumption_id,))

    def record_objection(self, assumption_id: int, *, envelope_id: int,
                         transport: str, sent_ts: float | None = None) -> None:
        """Record that an objection was handed to a transport"""
        assert transport in OBJECTION_TRANSPORTS, transport
        self.conn.execute(
            """UPDATE assumptions SET objection_envelope_id=?, objection_transport=?,
                   objection_sent_ts=? WHERE id=?""",
            (envelope_id, transport, db.now() if sent_ts is None else sent_ts,
             assumption_id))

    def assumption_for_envelope(self, envelope_id: int) -> dict[str, Any] | None:
        """The assumption whose objection rides that envelope, or None (§6.1)"""
        row = self.conn.execute(
            "SELECT * FROM assumptions WHERE objection_envelope_id=?",
            (envelope_id,)).fetchone()
        return dict(row) if row else None

    def mark_objection_delivered(self, assumption_id: int, ts: float | None = None,
                                 late_msg_id: int | None = None) -> None:
        """The worker ACTUALLY received it — not that the OS sent it"""
        self.conn.execute(
            "UPDATE assumptions SET objection_delivered_ts=?, objection_late_msg_id=? "
            "WHERE id=?",
            (db.now() if ts is None else ts, late_msg_id, assumption_id))

    def undelivered_objections(self, wo_id: str) -> list[dict[str, Any]]:
        """Objections never delivered, outstanding OR withdrawn — what a late message can carry"""
        return [a for a in self.all_assumptions(wo_id)
                if a.get("objection_envelope_id") is not None
                and a.get("objection_delivered_ts") is None]

    def withdraw_objection(self, assumption_id: int, ts: float | None = None) -> None:
        """The order stopped before the objection could be delivered (§6.6)"""
        self.conn.execute(
            "UPDATE assumptions SET objection_withdrawn_ts=? WHERE id=?",
            (db.now() if ts is None else ts, assumption_id))

    def work_orders_with_objections(self) -> list[str]:
        """Every work order an objection was ever filed against — ONE query"""
        rows = self.conn.execute(
            "SELECT DISTINCT wo_id FROM assumptions "
            "WHERE objection_envelope_id IS NOT NULL").fetchall()
        return [str(r["wo_id"]) for r in rows]

    def objection_counts(self, since: float | None = None) -> dict[str, Any]:
        """What this project's objections DID: sent, delivered, withdrawn, split by why"""
        from . import db as db_mod

        bound = "" if since is None else " AND objection_sent_ts >= ?"
        args: tuple[Any, ...] = () if since is None else (since,)
        row = self.conn.execute(
            "SELECT "
            " SUM(objection_sent_ts IS NOT NULL) AS sent,"
            " SUM(objection_delivered_ts IS NOT NULL) AS delivered,"
            " SUM(objection_withdrawn_ts IS NOT NULL) AS withdrawn"
            f" FROM assumptions WHERE objection_envelope_id IS NOT NULL{bound}",
            args).fetchone()
        counts = {"sent": int(row["sent"] or 0),
                  "delivered": int(row["delivered"] or 0),
                  "withdrawn": int(row["withdrawn"] or 0)}
        by_reason = {OBJECTION_WITHDRAWN_REASON: 0, OBJECTION_HANDED_BACK_REASON: 0,
                     "other": 0}
        ids = {int(r["id"]) for r in self.conn.execute(
            "SELECT id FROM assumptions WHERE objection_withdrawn_ts IS NOT NULL"
            f"{bound}", args).fetchall()}
        for event in self.conn.execute(
                "SELECT payload FROM wo_events "
                "WHERE kind='autoreview_objection_withdrawn' ORDER BY ts").fetchall():
            payload = db_mod.from_json(event["payload"], {})
            if int(payload.get("assumption_id") or 0) not in ids:
                continue
            reason = str(payload.get("reason") or "")
            by_reason[reason if reason in by_reason else "other"] += 1
        counts["by_reason"] = by_reason
        return counts

    def assumption_review_facts(self, since: float | None = None) -> dict[str, Any]:
        """Every assumption row since `since`, and what auto-review did to each (io-51469add)"""
        from . import db as db_mod

        bound = "" if since is None else " WHERE ts >= ?"
        args: tuple[Any, ...] = () if since is None else (since,)
        rows = db_mod.rows_to_dicts(self.conn.execute(
            "SELECT id, wo_id, ts, status, decided_by, neo_question_id,"
            f" confirm_question_id FROM assumptions{bound} ORDER BY ts, id",
            args).fetchall())
        asked: set[int] = set()
        last_hold: dict[int, str] = {}
        high_stakes: set[int] = set()
        false_alarms: set[int] = set()
        dropped = {"total": 0, "re_asked": 0}
        for event in self.conn.execute(
                "SELECT kind, payload FROM wo_events WHERE kind IN ('autoreview_asked',"
                " 'autoreview_held', 'autoreview_escalated',"
                " 'autoreview_stakes_false_alarm') ORDER BY ts").fetchall():
            payload = db_mod.from_json(event["payload"], {})
            aid = int(payload.get("assumption_id") or 0)
            kind = event["kind"]
            if kind == "autoreview_asked":
                asked.add(aid)
            elif kind == "autoreview_held":
                code = str(payload.get("code") or "")
                last_hold[aid] = code
                if code == "high_stakes":
                    high_stakes.add(aid)
            elif kind == "autoreview_stakes_false_alarm":
                false_alarms.add(aid)
            elif payload.get("dropped"):
                dropped["total"] += 1
                dropped["re_asked"] += 1 if payload.get("re_asked") else 0
        return {"rows": rows, "asked": asked, "last_hold": last_hold,
                "high_stakes": high_stakes, "false_alarms": false_alarms,
                "dropped": dropped}

    def delivered_objections(self, since: float | None = None) -> list[dict[str, Any]]:
        """Assumptions whose objection reached the worker, objection sent since `since`"""
        bound = "" if since is None else " AND objection_sent_ts >= ?"
        args: tuple[Any, ...] = () if since is None else (since,)
        return db.rows_to_dicts(self.conn.execute(
            "SELECT * FROM assumptions WHERE objection_envelope_id IS NOT NULL"
            f" AND objection_delivered_ts IS NOT NULL{bound}", args).fetchall())

    def outstanding_objections(self, wo_id: str) -> list[dict[str, Any]]:
        """Objections that are neither delivered nor withdrawn — ONE query, two readers"""
        return [a for a in self.all_assumptions(wo_id)
                if a.get("objection_envelope_id") is not None
                and a.get("objection_delivered_ts") is None
                and a.get("objection_withdrawn_ts") is None]

    # -- validation rounds (see the validation-panel design)

    @staticmethod
    def _subject(wo_id: str | None, fo_id: str | None) -> tuple[str, str]:
        """(column, id) for the one subject given"""
        if (wo_id is None) == (fo_id is None):
            raise ValueError("pass exactly one of wo_id / fo_id")
        return ("wo_id", wo_id) if wo_id is not None else ("fo_id", fo_id)  # type: ignore[return-value]

    def open_validation_round(self, *, wo_id: str | None = None,
                              fo_id: str | None = None, fingerprint: str,
                              summary: str = "", evidence: str = "",
                              pr_url: str | None = None,
                              round: int | None = None,
                              config_version: str | None = None,
                              forced_reason: str = "",
                              uncounted: bool = False,
                              uncounted_cause: str = "") -> dict[str, Any]:
        """Start a round on one subject, or return the one that already holds its number"""
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
                                                   config_version, forced_reason,
                                                   uncounted, uncounted_cause)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (subject_id, round, db.now(), fingerprint, summary, evidence, pr_url,
                 config_version, forced_reason, 1 if uncounted else 0,
                 uncounted_cause if uncounted else ""),
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
        """How many rounds this subject has actually SPENT"""
        col, subject_id = self._subject(wo_id, fo_id)
        marks = ",".join("?" * len(COUNTED_VALIDATION_OUTCOMES))
        row = self.conn.execute(
            f"SELECT COUNT(*) AS n FROM validation_rounds "
            f"WHERE {col}=? AND uncounted=0 AND outcome IN ({marks})",
            (subject_id, *COUNTED_VALIDATION_OUTCOMES),
        ).fetchone()
        return int(row["n"] or 0)

    def numbered_validation_rounds(self, *, wo_id: str | None = None,
                                   fo_id: str | None = None) -> int:
        """How many round NUMBERS this subject has already spent (docs/superpowers/specs/2026-09-27-a-conflict-resolution-the-os-asked-for-costs-no-round.md §4.1)"""
        col, subject_id = self._subject(wo_id, fo_id)
        counted = ",".join("?" * len(COUNTED_VALIDATION_OUTCOMES))
        runnable = ",".join("?" * len(RUNNABLE_VALIDATION_OUTCOMES))
        row = self.conn.execute(
            f"SELECT COUNT(*) AS n FROM validation_rounds WHERE {col}=? AND ("
            f"outcome IN ({counted}) "
            f"OR (uncounted=1 AND outcome NOT IN ({runnable})))",
            (subject_id, *COUNTED_VALIDATION_OUTCOMES, *RUNNABLE_VALIDATION_OUTCOMES),
        ).fetchone()
        return int(row["n"] or 0)

    def uncounted_validation_rounds(self, *, wo_id: str | None = None,
                                    fo_id: str | None = None,
                                    cause: str | None = None) -> int:
        """How many rounds this subject has had outside the budget, by cause"""
        col, subject_id = self._subject(wo_id, fo_id)
        where, args = "", [subject_id]
        if cause is not None:
            where, args = " AND uncounted_cause=?", [subject_id, cause]
        row = self.conn.execute(
            f"SELECT COUNT(*) AS n FROM validation_rounds "
            f"WHERE {col}=? AND uncounted=1{where}", args).fetchone()
        return int(row["n"] or 0)

    def get_validation_round(self, round_id: int) -> dict[str, Any] | None:
        row = self.conn.execute(
            "SELECT * FROM validation_rounds WHERE id=?", (round_id,)).fetchone()
        return dict(row) if row else None

    def mark_validation_round_uncounted(self, round_id: int, cause: str) -> None:
        """Take an OPEN round out of the budget (docs/superpowers/specs/2026-10-13-a-red-head-is-sent-back-before-the-panel.md §3.3)"""
        self.conn.execute(
            "UPDATE validation_rounds SET uncounted=1, uncounted_cause=? WHERE id=?",
            (cause, round_id),
        )

    def close_validation_round(self, round_id: int, outcome: str,
                               reason: str = "",
                               hold_cause: str | None = None) -> None:
        """Close a round"""
        assert outcome in VALIDATION_OUTCOMES, outcome
        assert hold_cause is None or hold_cause in VALIDATION_HOLDING_CAUSES, hold_cause
        self.conn.execute(
            "UPDATE validation_rounds SET outcome=?, reason=?, hold_cause=? WHERE id=?",
            (outcome, reason, hold_cause, round_id),
        )

    def settle_validation_round(self, round_id: int, outcome: str,
                                payload: dict[str, Any] | None = None, *,
                                wo_id: str = "", fo_id: str = "", reason: str = "",
                                hold_cause: str | None = None,
                                kind: str = "") -> bool:
        """Settle a round: the outcome and its timeline event in ONE commit (#1203)"""
        unit = wo_id
        if fo_id:
            manager = self.manager_work_order(fo_id)
            unit = str(manager["id"]) if manager else ""
        with db.write_transaction(self.conn):
            self.close_validation_round(round_id, outcome, reason, hold_cause)
            if unit:
                self.add_event(unit, kind or f"validation_{outcome}", payload or {})
        return bool(unit)

    def set_validation_head(self, round_id: int, head_sha: str) -> None:
        """Record which commit this round is judging"""
        self.conn.execute("UPDATE validation_rounds SET head_sha=? WHERE id=?",
                          (head_sha, round_id))

    def set_validation_file_shas(self, round_id: int,
                                 file_shas: Iterable[tuple[str, str]]) -> None:
        """Record what each file looked like in the diff this round judged"""
        self.conn.execute("UPDATE validation_rounds SET file_shas=? WHERE id=?",
                          (db.to_json(dict(file_shas)), round_id))

    @staticmethod
    def validation_file_shas(round_row: Mapping[str, Any] | None) -> dict[str, str]:
        """One round's per-file digests, `{}` when it recorded none"""
        raw = db.from_json(str((round_row or {}).get("file_shas") or ""), {})
        return {str(k): str(v) for k, v in raw.items()} if isinstance(raw, dict) else {}

    def last_judged_round(self, *, wo_id: str | None = None,
                          fo_id: str | None = None) -> dict[str, Any] | None:
        """The most recent round a PANEL actually settled, or None"""
        for row in reversed(self.validation_rounds(wo_id=wo_id, fo_id=fo_id)):
            if str(row["outcome"] or "") in COUNTED_VALIDATION_OUTCOMES:
                return row
        return None

    @staticmethod
    def validated_head(round_row: dict[str, Any] | None) -> str | None:
        """The commit the panel ACCEPTED, given its LATEST round, or None (docs/superpowers/specs/2026-09-18-a-red-base-heals-itself.md §5)"""
        if round_row is None or round_row["outcome"] != "passed":
            return None
        judged = str(round_row["head_sha"] or "")
        if not judged:
            # A carry can never manufacture a binding where the seats bound none: the whole licence for carrying...
            return None
        # `.get`, unlike the two lookups above: this column arrived after the rule did
        return str(round_row.get("carried_head_sha") or "") or judged

    def validation_rounds(self, *, wo_id: str | None = None,
                          fo_id: str | None = None) -> list[dict[str, Any]]:
        """Every round on one subject, oldest first — the order they were judged in"""
        col, subject_id = self._subject(wo_id, fo_id)
        return db.rows_to_dicts(self.conn.execute(
            f"SELECT * FROM validation_rounds WHERE {col}=? ORDER BY round",
            (subject_id,),
        ).fetchall())

    def work_orders_awaiting_validation(self) -> list[dict[str, Any]]:
        """Every OPEN work order whose latest round is the round machine's to run (docs/superpowers/specs/2026-09-13-two-gates-not-a-chain.md §3)"""
        marks = ",".join("?" * len(RUNNABLE_VALIDATION_OUTCOMES))
        statuses = ",".join("?" * len(OPEN_STATUSES))
        rows = self.conn.execute(
            f"""SELECT w.* FROM work_orders w
                  JOIN validation_rounds r ON r.wo_id = w.id
                 WHERE r.outcome IN ({marks})
                   AND w.status IN ({statuses})
                   AND r.round = (SELECT MAX(round) FROM validation_rounds
                                   WHERE wo_id = w.id)
                 ORDER BY w.created_at""",
            (*RUNNABLE_VALIDATION_OUTCOMES, *OPEN_STATUSES),
        ).fetchall()
        return db.rows_to_dicts(rows)

    @staticmethod
    def round_machine_owns(round_row: dict[str, Any] | None) -> bool:
        """Does the round machine still own the work order this row is the latest round of?"""
        return str((round_row or {}).get("outcome") or "") in RUNNABLE_VALIDATION_OUTCOMES

    def validation_round_open(self, wo_id: str) -> bool:
        """Is the round machine going to act on this work order? (docs/superpowers/specs/2026-09-13-a-work-order-never-sits-on-a-red-pull-request.md)"""
        return self.round_machine_owns(self.latest_validation_round(wo_id=wo_id))

    def latest_validation_round(self, *, wo_id: str | None = None,
                                fo_id: str | None = None) -> dict[str, Any] | None:
        """The most recent round on one subject, or None if it has never been judged"""
        col, subject_id = self._subject(wo_id, fo_id)
        row = self.conn.execute(
            f"SELECT * FROM validation_rounds WHERE {col}=? ORDER BY round DESC LIMIT 1",
            (subject_id,),
        ).fetchone()
        return dict(row) if row else None

    def carry_round_head(self, round_id: int, head_sha: str, reason: str) -> None:
        """Bind a passed round's verdict to a commit the OS itself produced"""
        self.conn.execute(
            "UPDATE validation_rounds SET carried_head_sha=?, carried_reason=? WHERE id=?",
            (head_sha, reason, round_id))

    def record_validation_opinion(self, round_id: int, seat: str, *, reply: str = "",
                                  verdict: str = "", status: str = "ok",
                                  model: str = "", latency_ms: int = 0) -> None:
        """What one seat said"""
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
        """One round's opinions, in the order the seats first reported"""
        return db.rows_to_dicts(self.conn.execute(
            "SELECT * FROM validation_opinions WHERE round_id=? ORDER BY id",
            (round_id,),
        ).fetchall())

    # -- approvals (privileged-action gates; see gates.py)

    def add_approval(self, wo_id: str, kind: str, command: str, matched: str = "",
                     justification: str = "", evidence: str = "",
                     max_uses: int = 3,
                     agent_type: str | None = None,
                     status: str = "pending",
                     contested: bool = False,
                     base_oid: str = "") -> dict[str, Any]:
        now = db.now()
        cur = self.conn.execute(
            """INSERT INTO approvals (wo_id, ts, kind, command, matched, justification,
                                      evidence, max_uses, agent_type, status, contested,
                                      pending_at, base_oid)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            # `pending_at` iff it is pending from birth: an `awaiting_case` row gets one in `start_review` or...
            (wo_id, now, kind, command, matched, justification, evidence, max_uses,
             agent_type, status, int(contested), now if status == "pending" else None,
             base_oid),
        )
        approval_id = int(cur.lastrowid)  # type: ignore[arg-type]
        self.add_event(wo_id, "gate_requested", {
            "approval_id": approval_id, "kind": kind, "command": command,
            # Whether a reviewer can see it yet
            "held": status == "awaiting_case",
            # A CONTEST, not a request: the worker is disputing the match
            **({"contested": True} if contested else {}),
            # In the payload as well as the column: the timeline is read on its own
            **({"agent_type": agent_type} if agent_type else {}),
            # Spec 2026-09-28 §3.5: only when there is one — `''` is "no base recorded"
            **({"base_oid": base_oid} if base_oid else {}),
        })
        return self.get_approval(approval_id)  # type: ignore[return-value]

    def start_review(self, approval_id: int, question_id: int) -> dict[str, Any]:
        """Link the reviewer's question and open the request to review"""
        self.conn.execute(
            """UPDATE approvals SET neo_question_id=?, status='pending',
                                    pending_at=COALESCE(pending_at, ?) WHERE id=?""",
            (question_id, db.now(), approval_id),
        )
        return self.get_approval(approval_id)  # type: ignore[return-value]

    def get_approval(self, approval_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM approvals WHERE id=?", (approval_id,)).fetchone()
        return dict(row) if row else None

    def latest_approval_for(self, wo_id: str, kind: str, command: str
                            ) -> dict[str, Any] | None:
        """The most recent request this work order filed for this exact command"""
        row = self.conn.execute(
            """SELECT * FROM approvals WHERE wo_id=? AND kind=? AND command=?
               ORDER BY ts DESC, id DESC LIMIT 1""",
            (wo_id, kind, command),
        ).fetchone()
        return dict(row) if row else None

    def amend_approval(self, approval_id: int, justification: str,
                       evidence: str) -> dict[str, Any]:
        """Write a fuller case onto a request that is still pending"""
        approval = self.get_approval(approval_id)
        if approval is None:
            raise KeyError(f"approval {approval_id} not found")
        self.conn.execute(
            "UPDATE approvals SET justification=?, evidence=? WHERE id=?",
            (justification, evidence, approval_id),
        )
        self.add_event(approval["wo_id"], "gate_amended", {
            "approval_id": approval_id, "kind": approval["kind"],
            "command": approval["command"],
        })
        return self.get_approval(approval_id)  # type: ignore[return-value]

    def link_neo_question(self, approval_id: int, question_id: int) -> None:
        self.conn.execute("UPDATE approvals SET neo_question_id=? WHERE id=?",
                          (question_id, approval_id))

    def approval_for_question(self, question_id: int) -> dict[str, Any] | None:
        row = self.conn.execute("SELECT * FROM approvals WHERE neo_question_id=?",
                                (question_id,)).fetchone()
        return dict(row) if row else None

    def mark_approval_escalated(self, approval_id: int, reason: str) -> None:
        """Neo declined to decide: the request stays open, now against the user"""
        self.conn.execute(
            "UPDATE approvals SET escalated=1, escalation_reason=? WHERE id=?",
            (reason, approval_id),
        )

    def escalated_approvals(self, wo_id: str | None = None) -> list[dict[str, Any]]:
        """Requests waiting on the user specifically — the only gates that are allowed to consume attention"""
        return [a for a in self.pending_approvals(wo_id) if a["escalated"]]

    def decide_approval(self, approval_id: int, verdict: str, reason: str,
                        decided_by: str, ttl_seconds: int = 3600) -> dict[str, Any]:
        """Record a verdict — `approved`, `denied` or `dismissed`"""
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
        """Close a pending request that no longer has an answer worth giving"""
        approval = self.get_approval(approval_id)
        if approval is None:
            raise KeyError(f"approval {approval_id} not found")
        # `awaiting_case` too: a request nobody ever argued is no more answerable than a pending one once its...
        if approval["status"] not in ("pending", "awaiting_case"):
            return approval
        self.conn.execute(
            """UPDATE approvals SET status='expired', closed_as='superseded',
                                    decided_by='os', decision_reason=?, decided_at=?,
                                    expires_at=NULL
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
        """The decided request that lets this exact command through right now, or None"""
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
        """Spend one use of a grant"""
        self.conn.execute("UPDATE approvals SET uses = uses + 1 WHERE id=?", (approval_id,))
        approval = self.get_approval(approval_id)
        assert approval is not None
        self.add_event(approval["wo_id"], "gate_opened", {
            "approval_id": approval_id,
            "kind": approval["kind"],
            "use": approval["uses"],
            "of": approval["max_uses"],
            # Which of the two clearing statuses opened it
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

    def approvals_of_kind(self, kind: str, status: str) -> list[dict[str, Any]]:
        """Every approval of ONE kind at ONE status, newest first"""
        rows = self.conn.execute(
            "SELECT * FROM approvals WHERE kind=? AND status=? ORDER BY ts DESC, id DESC",
            (kind, status),
        ).fetchall()
        return db.rows_to_dicts(rows)

    def pending_approvals(self, wo_id: str | None = None) -> list[dict[str, Any]]:
        """Requests still awaiting a verdict — from Neo or, once escalated, the user"""
        return self.list_approvals(wo_id, statuses=("pending",))

    def held_approvals(self, wo_id: str | None = None) -> list[dict[str, Any]]:
        """Requests recorded but not yet argued, so not yet in front of anyone"""
        return self.list_approvals(wo_id, statuses=("awaiting_case",))

    def open_approvals(self, wo_id: str | None = None) -> list[dict[str, Any]]:
        """Every request still on its way to a verdict, held or pending"""
        return self.pending_approvals(wo_id) + self.held_approvals(wo_id)

    def gate_open_at(self, wo_id: str, ts: float) -> bool:
        """Was a privileged-action gate REFUSING THIS WORKER'S COMMANDS at `ts`? (#469)"""
        for approval in self.list_approvals(wo_id):
            pending_at = approval["pending_at"]
            if pending_at is None or pending_at > ts:
                continue
            decided_at = approval["decided_at"]
            if decided_at is None or decided_at > ts:
                return True
        return False

    def expire_approvals(self) -> int:
        """Move spent or timed-out grants to `expired` so listings tell the truth"""
        spent = self.conn.execute(
            """UPDATE approvals SET status='expired', closed_as='spent'
               WHERE status='approved' AND uses > 0 AND uses >= max_uses""",
        )
        lapsed = self.conn.execute(
            """UPDATE approvals SET status='expired', closed_as='lapsed'
               WHERE status='approved'
                 AND (uses >= max_uses OR (expires_at IS NOT NULL AND expires_at < ?))""",
            (db.now(),),
        )
        return spent.rowcount + lapsed.rowcount

    def abandon_approval(self, approval_id: int, reason: str) -> dict[str, Any]:
        """Close a held request whose case never came"""
        approval = self.get_approval(approval_id)
        if approval is None:
            raise KeyError(f"approval {approval_id} not found")
        if approval["status"] != "awaiting_case":
            return approval
        self.conn.execute(
            """UPDATE approvals SET status='expired', closed_as='abandoned',
                                    decided_by='os', decision_reason=?, decided_at=?,
                                    expires_at=NULL
               WHERE id=?""",
            (reason, db.now(), approval_id),
        )
        self.add_event(approval["wo_id"], "gate_abandoned", {
            "approval_id": approval_id,
            "kind": approval["kind"],
            "command": approval["command"],
            "matched": approval["matched"],
            "reason": reason,
        })
        return self.get_approval(approval_id)  # type: ignore[return-value]

    def mark_contested(self, approval_id: int) -> dict[str, Any]:
        """Record that the worker disputes the MATCH rather than asking permission"""
        self.conn.execute("UPDATE approvals SET contested=1 WHERE id=?", (approval_id,))
        return self.get_approval(approval_id)  # type: ignore[return-value]

    def abandoned_count(self, wo_id: str | None = None) -> int:
        """How many held requests timed out with no case and no contest"""
        q = ("SELECT COUNT(*) c FROM approvals "
             "WHERE status='expired' AND closed_as='abandoned'")
        params: list[Any] = []
        if wo_id:
            q += " AND wo_id=?"
            params.append(wo_id)
        return int(self.conn.execute(q, params).fetchone()["c"])

    def dismissed_count(self, wo_id: str | None = None) -> int:
        """How many gate requests turned out not to be gated actions at all"""
        q = "SELECT COUNT(*) c FROM approvals WHERE status='dismissed'"
        params: list[Any] = []
        if wo_id:
            q += " AND wo_id=?"
            params.append(wo_id)
        return int(self.conn.execute(q, params).fetchone()["c"])

    # -- summary

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
