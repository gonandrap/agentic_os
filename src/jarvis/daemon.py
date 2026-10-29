"""jarvisd — the deterministic OS daemon"""

from __future__ import annotations

import dataclasses
import hashlib
import logging
import os
import re
import signal
import time
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import (background, bugreport, bus, claude_cli, db, fleet, holds, inspection,
               notify, release, usage_meter, verdicts, worker_session)
from . import budget as budget_mod
from . import steward
from .catalog import DEFAULT_STAKES_CLASSIFIER, Catalog, ProjectSpec, load_catalog
from .central_store import ARCHIVED, SETTLED_BACKLOG_STATUSES, CentralStore
from .dispatch import dispatch_work_order
from . import invariants as invariants_mod
from .invariants import PR_REPAIR_STATUSES, RETRY_HELD_RESTATE
from .neo_store import QuestionTooLargeError
from .paths import daemon_pidfile, ensure_home, logs_dir
# The `os_state` keys the dashboard's self-heal keeps
from .uilog import UI_HEALTHZ_OK_AT, UI_WEDGE_CAPPED_AT, UI_WEDGE_RESTARTS
from .project_store import (
    COUNTED_VALIDATION_OUTCOMES,
    FO_OPEN_STATUSES,
    FO_TERMINAL_STATUSES,
    MAX_MESSAGE_DELIVERY_ATTEMPTS,
    OBJECTION_HANDED_BACK_REASON,
    OBJECTION_WITHDRAWN_REASON,
    OPEN_STATUSES,
    PR_PENDING_EVENT,
    RETRY_SWEEP_STATUSES,
    RUNNABLE_VALIDATION_OUTCOMES,
    TERMINAL_STATUSES,
    UNGOVERNED_ORIGINS,
    VALIDATION_AUTH_CAUSE,
    VALIDATION_CI_CAUSE,
    VALIDATION_HELD_CAUSE,
    WORKER_CHILDREN,
    ProjectStore,
    resume_spends_slot,
    validation_hold_until,
)

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .evidence import EvidencePacket

log = logging.getLogger("jarvisd")

#: The objection withdrawal rate's window, and the sample below which it is noise
OBJECTION_ALARM_WINDOW_DAYS = 7
OBJECTION_ALARM_MIN_SENT = 3

#: Why an objection was taken back


def fetch_healthz(url: str, timeout: float) -> dict[str, Any] | None:
    """`GET /healthz` as a dict, or None when the dashboard did not answer"""
    import urllib.error
    import urllib.request

    try:
        try:
            with urllib.request.urlopen(url, timeout=timeout) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as e:
            raw = e.read()
        body = db.from_json(raw.decode("utf-8", errors="replace"), None)
    except Exception:  # noqa: BLE001 — every other failure is one verdict (spec §4.2)
        return None
    return body if isinstance(body, dict) else None


def _limiter_line(figures: dict[str, Any]) -> str:
    """The anyio capacity limiter, as the inbox item states it"""
    return (f"borrowed {figures.get('borrowed', '?')} / available "
            f"{figures.get('available', '?')} of {figures.get('total', '?')}, "
            f"{figures.get('waiting', '?')} waiting")


def _existing(path: Path) -> str:
    """A file the OS can cite, or `""` — a path is not evidence that it was written"""
    try:
        return str(path) if path.exists() else ""
    except OSError:
        return ""


def _wedge_evidence(figures: dict[str, Any], dump: str) -> str:
    """What the inbox item may claim: only a dump and stamp that EXIST (issue #1189)"""
    from . import uilog

    lines = [f"Limiter: {_limiter_line(figures)}",
             f"Stacks: {dump or 'none — no stack dump was written'}"]
    # A loop stall and a wedged threadpool are different faults; the stamp says which (issue #1221)
    reason = str((uilog.read_wedge() or {}).get("reason") or "")
    if reason:
        lines.append(f"Reason: {reason}")
    stamp = _existing(uilog.wedge_stamp_path())
    if stamp:
        lines.append(f"Stamp: {stamp}")
    return "\n".join(lines)


RECONCILE_EVERY_TICKS = 6  # refresh `claude agents --json` every N ticks (injected only)

#: Reconcile stamp an outside reader judges liveness by; spec 2026-10-16 §2a
RECONCILE_TICK_KEY = "reconcile_tick"

#: Holds the low-disk warning, so it is raised once and re-armed only on recovery (docs/superpowers/specs/2026-10-22-a-settled-checkout-is-reclaimed.md §2)
DISK_LOW_KEY = "disk_low_warned"
SECONDS_PER_HOUR = 3600    # a unit, not a setting
#: Ask GitHub about parked pull requests every N ticks — ~2 minutes at the default 5s interval
PR_POLL_EVERY_TICKS = 24
#: One Morpheus pass every N ticks (~1 minute at the default 5s poll)
MORPHEUS_EVERY_TICKS = 12

#: Read the account's usage meter every N ticks — one minute at the default 5s interval (docs/superpowers/specs/2026-10-08-usage-meter-samples-and-outside-spend.md)
USAGE_SAMPLE_EVERY_TICKS = 12

#: Re-offer the steward's queued self-work every N ticks — ten minutes at the default 5s (#1181)
STEWARD_DRAIN_EVERY_TICKS = 120

#: How often orders that have stopped moving are swept is `fleet_health.sweep_every_ticks`

#: Which work orders' pull requests get asked about (#224)
PR_POLL_STATUSES = PR_REPAIR_STATUSES + ("validating",)

#: Look for a work order the transport parked
RETRY_EVERY_TICKS = 2

#: Sweep for completed work orders whose pull request never merged every N ticks (#232)
LANDING_SWEEP_EVERY_TICKS = 720

#: How many open orders one `rules_tick` will look at (docs/superpowers/specs/2026-09-27-self-evolution.md)
EVAL_MAX_ORDERS = 200

#: …and the other half of the cap, because the order count is not the cost
EVAL_MAX_SECONDS = 5.0

#: Look at the scheduler's clock every N ticks — a minute at the default 5s interval
SCHEDULE_EVERY_TICKS = 12

#: The status each kind of family RUNS in (docs/superpowers/specs/2026-10-01-a-family-capped-raise-must-say-so.md)
_FAMILY_RUNNING_STATUS = {
    "feature": "executing",
    "improvement": "planning",
    "investigation": "planning",
}

#: Which statuses `Daemon.settle_turns` sweeps (docs/superpowers/specs/2026-10-09-derive-somebody-is-going-to-act-on-this.md)
SETTLE_STATUSES = ("running", "idle", "waiting_input", "dispatching")

#: How long after a claim `settle_work_order` waits for the launch to record its turn
TURN_CLAIM_GRACE_SECONDS = 300


def settle_grace_holds(wo: dict[str, Any], now: float | None = None) -> bool:
    """Still inside the post-claim grace — section 3 of the 2026-10-09 spec"""
    return (now or time.time()) - wo["updated_at"] <= TURN_CLAIM_GRACE_SECONDS


def settles_turn(store: ProjectStore, wo: dict[str, Any],
                 now: float | None = None) -> bool:
    """True when `Daemon.settle_turns` will act on this row — 2026-10-09 spec §3"""
    if wo["status"] not in SETTLE_STATUSES:
        return False
    if wo["origin"] in UNGOVERNED_ORIGINS:
        return False
    if settle_grace_holds(wo, now):
        return True
    turn = store.latest_turn(wo["id"])
    if turn is None:
        return False  # the claim-with-no-launch hole; see the docstring above
    if turn["state"] != "done":
        return True   # running or paused: the settler re-derives the row from the turn
    return settles_done_turn(store, wo)


def settles_done_turn(store: ProjectStore, wo: dict[str, Any], *,
                      queued: list[dict[str, Any]] | None = None) -> bool:
    """Does a DONE turn move this row — `Daemon.settle_work_order`'s own rule (docs/superpowers/specs/2026-09-29-a-heredoc-edit-is-not-a-merge.md)"""
    from .invariants import something_is_out, user_facing_wait

    msgs = queued if queued is not None else store.queued_messages(wo["id"])
    if msgs:
        return False
    if wo.get("result_summary"):
        return True
    manager = wo.get("kind") == "manager"
    if not manager and something_is_out(store, wo["id"]):
        return (user_facing_wait(store, wo["id"])
                and wo["status"] != "waiting_input")
    if manager:
        # Its feature being over is the one thing that still moves it (`_close_feature_manager`)
        parent = wo.get("parent_id")
        feature = store.get_feature_order(parent) if parent else None
        if feature and feature["status"] in FO_TERMINAL_STATUSES:
            return True
        if user_facing_wait(store, wo["id"]):
            return False
        return wo["status"] != "idle"
    return True


def retries_turn(store: ProjectStore, wo: dict[str, Any],
                 now: float | None = None) -> worker_session.TurnPause | None:
    """The pause `Daemon.retry_paused_turns` will relaunch, or None — 2026-10-09 §3"""
    if wo["status"] not in RETRY_SWEEP_STATUSES:
        return None
    if wo["origin"] in UNGOVERNED_ORIGINS:
        return None
    pause = worker_session.turn_pause(store, wo["id"])
    if pause is not None and pause.resumable and pause.due(now=now):
        return pause
    return None


def delivers_message(store: ProjectStore, wo: dict[str, Any],
                     now: float | None = None, *,
                     queued: list[dict[str, Any]] | None = None) -> bool:
    """True when `Daemon.deliver_messages` will send a queued turn — 2026-10-09 §3 (docs/superpowers/specs/2026-10-20-an-objection-is-withdrawn-before-the-order-stops.md)"""
    msgs = queued if queued is not None else store.queued_messages(wo["id"])
    if not msgs:
        return False
    return any(worker_session.message_hold(store, wo, m, now=now) is None for m in msgs)


def validation_round(store: ProjectStore, wo: dict[str, Any],
                     now: float | None = None) -> dict[str, Any] | None:
    """The round THE PANEL OWNS on this row, held or not — 2026-10-09 §2"""
    if wo["status"] not in OPEN_STATUSES:
        return None
    row = store.latest_validation_round(wo_id=wo["id"])
    if not row or row.get("outcome") not in RUNNABLE_VALIDATION_OUTCOMES:
        return None
    return row


def runnable_round(store: ProjectStore, wo: dict[str, Any],
                   now: float | None = None) -> dict[str, Any] | None:
    """The round `Daemon.validation_tick` will judge NOW, or None — 2026-10-09 §3"""
    row = validation_round(store, wo, now)
    if row is None:
        return None
    if validation_hold_until(store.events_of_kind(wo["id"], "validation_failed"),
                             int(row["round"])) > (now or time.time()):
        return None
    return row


def polls_pull_request(store: ProjectStore, wo: dict[str, Any],
                       now: float | None = None, *,
                       recorded: Any = None) -> bool:
    """True when `Daemon.poll_pull_requests` will ask GitHub about this row — §3"""
    if wo["status"] not in PR_POLL_STATUSES:
        return False
    if not wo.get("pr_url"):
        return False
    if recorded is not None and wo["id"] not in recorded:
        return True
    from . import ops
    return bool(ops.routes_on_pull_request(store, wo))


def _minutes_label(minutes: int) -> str:
    """`120` -> `2h`, `90` -> `90m`"""
    return f"{minutes // 60}h" if minutes % 60 == 0 else f"{minutes}m"


def _seconds_label(seconds: int) -> str:
    """`600` -> `10m`, `90` -> `90s`"""
    return f"{seconds // 60}m" if seconds % 60 == 0 else f"{seconds}s"


#: The timeline's record that GitHub has stopped recomputing one pull request
PR_RECOMPUTE_STALLED_EVENT = "pr_recompute_stalled"


def pr_recompute_stalled(store: ProjectStore, wo: dict[str, Any],
                         now: float | None = None) -> bool:
    """True when GitHub has stopped recomputing this pull request — wo-d3a70c29 (#995)"""
    ref = str(wo.get("pr_ref_oid") or "")
    head = str(wo.get("pr_head_oid") or "")
    if ref and head and ref != head:
        return True
    since = wo.get("pr_mergeable_unknown_since")
    if not isinstance(since, (int, float)):
        return False
    from . import ops
    minutes = int(ops.pull_requests_config_at(
        store.project_path).recompute_stall_minutes)
    return (now or time.time()) - float(since) > minutes * 60


#: The one-shot resync episode, as four timeline kinds and NOTHING ELSE
PR_RESYNC_STARTED_EVENT = "pr_resync_started"
PR_RESYNC_DONE_EVENT = "pr_resync_done"
PR_RESYNC_FAILED_EVENT = "pr_resync_failed"
PR_RESYNC_CLEARED_EVENT = "pr_resync_cleared"


def _opened_episode(store: ProjectStore, wo: dict[str, Any],
                    kind: str) -> float | None:
    """When the latest `kind` event opened an episode, or None if there is none"""
    rows = store.events_of_kind(wo["id"], kind)
    return float(rows[-1]["ts"]) if rows else None


def _answered_since(store: ProjectStore, wo: dict[str, Any], kind: str,
                    since: float) -> dict[str, Any] | None:
    """The latest `kind` event at or after `since`, or None — the answering half"""
    rows = store.events_of_kind(wo["id"], kind)
    if rows and float(rows[-1]["ts"]) >= since:
        return rows[-1]
    return None


def pr_resync_owed(store: ProjectStore, wo: dict[str, Any]) -> bool:
    """True when this stall episode has not had its ONE close+reopen yet — wo-7ec57748"""
    stalled = _opened_episode(store, wo, PR_RECOMPUTE_STALLED_EVENT)
    if stalled is None:
        return False
    return _answered_since(store, wo, PR_RESYNC_STARTED_EVENT, stalled) is None


def pr_resync_unreopened(store: ProjectStore, wo: dict[str, Any]) -> bool:
    """True when the OS closed this pull request and has not got it open again"""
    started = _opened_episode(store, wo, PR_RESYNC_STARTED_EVENT)
    if started is None:
        return False
    if _answered_since(store, wo, PR_RESYNC_DONE_EVENT, started):
        return False
    failed = _answered_since(store, wo, PR_RESYNC_FAILED_EVENT, started)
    return not (failed and (db.from_json(failed["payload"], {}) or {}
                            ).get("stage") == "close")


def pr_resync_open(store: ProjectStore, wo: dict[str, Any]) -> bool:
    """True when a resync episode is still open — attempted and not yet cleared"""
    started = _opened_episode(store, wo, PR_RESYNC_STARTED_EVENT)
    if started is None:
        return False
    return _answered_since(store, wo, PR_RESYNC_CLEARED_EVENT, started) is None


def polls_and_can_act_on_pull_request(store: ProjectStore, wo: dict[str, Any],
                                      now: float | None = None, *,
                                      recorded: Any = None) -> bool:
    """`waiting_pr_merge`'s actor test: the poll selects this row AND can still move it"""
    return (polls_pull_request(store, wo, now=now, recorded=recorded)
            and not pr_recompute_stalled(store, wo, now=now))


def answers_question(store: ProjectStore, wo: dict[str, Any],
                     now: float | None = None) -> dict[str, Any] | None:
    """The question Neo still holds for this row, or None — 2026-10-09 §3"""
    from . import invariants
    q = invariants.awaiting_neo(wo["id"])
    from .neo_store import NEO_HELD_Q_STATUSES
    if q is not None and q["status"] in NEO_HELD_Q_STATUSES:
        return q
    return None


def reviews_gate(store: ProjectStore, wo: dict[str, Any],
                 now: float | None = None) -> bool:
    """True when a privileged-action request the user does not hold is open — §3"""
    from . import invariants
    return invariants.waiting_on_neo_gate(store, wo)


#: Walk the transcript tree for one-hour cache writes every N ticks
CACHE_TTL_EVERY_TICKS = 4320

#: WHICH tick of each period, and the only cadence here that is not `== 1`
CACHE_TTL_TICK_OFFSET = 60

#: Reconcile the meter against measured spend every N ticks — 30 minutes at the default 5s interval (docs/superpowers/specs/2026-10-08-usage-meter-samples-and-outside-spend.md)
METER_RECONCILE_EVERY_TICKS = 360

#: The two kinds `check_meter_residual` raises, and the inbox title of each
METER_OUTSIDE_ALARM = "cost_outside_spend_high"
METER_RESIDUAL_ALARM = "cost_residual_high"
METER_INBOX_TITLE = {
    METER_OUTSIDE_ALARM: "sessions Jarvis did not dispatch are spending the window",
    METER_RESIDUAL_ALARM: "the usage meter and the measured spend do not reconcile",
}

#: How many outside sessions the outside alarm NAMES
METER_ALARM_SESSIONS = 3


def meter_alarm_reason(kind: str, meter: dict[str, Any]) -> str:
    """One alarm's reason, written from the subtree the user can read for themselves"""
    from . import usage_meter as meter_mod

    spend = meter["spend"]
    implied = float(spend["implied_usd"] or 0.0)
    if kind == METER_RESIDUAL_ALARM:
        return (f"${spend['residual_usd']:.2f} of the ~${implied:.2f} implied by the 5h "
                f"meter ({round(100 * (spend['residual_share'] or 0.0))}%) is "
                f"unexplained — {meter_mod.RESIDUAL_LABEL}.")
    rows = meter["outside"]["sessions"][:METER_ALARM_SESSIONS]
    named = "; ".join(
        f"{row['session_id'][:8]} in {row['project'] or row['project_dir']} "
        f"(${row['usd']:.2f})" + (f' — "{row["title"]}"' if row["title"] else "")
        for row in rows)
    return (f"sessions on this machine Jarvis did not dispatch spent "
            f"${meter['outside']['total_usd']:.2f} of the ~${implied:.2f} implied by "
            f"the 5h meter ({round(100 * (spend['outside_share'] or 0.0))}%). "
            f"Dearest first: {named}.")

#: How many dashboard digests one batch may produce
DIGEST_BATCH = 5

#: THE VALIDATION SEAM
Validator = Callable[[ProjectStore, dict, "EvidencePacket"], dict]

#: THE REJECTION, WORDED ONCE
REVIEW_FEEDBACK = """REVIEW FEEDBACK (round {n} of {max})
{reason}

Address this and then run `jarvis wo finish {wo_id} --summary "..." --evidence "..."`
again. Re-submitting without changed code or new evidence will end the review."""

#: THE SAME REJECTION ON A ROUND NOBODY WAS CHARGED FOR (docs/superpowers/specs/2026-09-27-a-conflict-resolution-the-os-asked-for-costs-no-round.md §4.4)
REBIND_FEEDBACK = """REVIEW FEEDBACK (re-judgement {used} of {max}, no round spent)
{reason}

This is not one of your {max_rounds} review rounds. The OS asked you to merge the base
branch in, your resolution changed what this pull request contributes, and a seat has
read it. Fix what is above and run
`jarvis wo finish {wo_id} --summary "..." --evidence "..."` again."""

#: THE SAME REJECTION ON THE ROUND A USER REJECTION BOUGHT
USER_REWORK_FEEDBACK = """REVIEW FEEDBACK (no round spent)
{reason}

This is not one of your {max_rounds} review rounds. The user rejected this on review and
asked for the rework, so it was judged outside the budget. Fix what is above and run
`jarvis wo finish {wo_id} --summary "..." --evidence "..."` again — that next round is a
charged one."""

#: THE SAME REJECTION ON A HEAD GITHUB CALLED RED, with nobody seated and no round spent (docs/superpowers/specs/2026-09-26-bounded-model-inputs.md)
RED_CI_FEEDBACK = """REVIEW FEEDBACK (no round spent)
{reason}

Read the failures yourself with `gh pr checks {pr_url}` — none of the output is copied
here. Then either fix the build, or, if this branch cannot pass on its own because it
depends on another pull request that has not merged yet, stack it onto that branch or wait
for the merge. Either way, run
`jarvis wo finish {wo_id} --summary "..." --evidence "..."` again when the checks are
green.

This is not one of your {max_rounds} review rounds and nobody has read the work yet, so
nothing was spent here. The next submission is judged normally."""

#: THE SAME REJECTION, ADDRESSED TO A DIFFERENT JOB
FEATURE_REVIEW_FEEDBACK = """REVIEW FEEDBACK ON THE FEATURE (round {n} of {max})
{reason}

Decide what actually has to change, file a work order under {fo_id} for each thing that
does (`jarvis wo create <project> "..." --parent {fo_id}`), and once they have landed run
`jarvis fo submit {fo_id} --summary "..." --evidence "..."`. Re-submitting without changed
code or new evidence will end the review."""

#: What an idle manager is told when the work orders it filed after a rejection have all completed
FEATURE_CHILDREN_LANDED = """Everything you filed under {fo_id} has completed and its
code is on the default branch, so the feature is ready to go back to the panel.

If the review's feedback is addressed, run `jarvis fo submit {fo_id} --summary "..."
--evidence "..."`. If it is not, file the work orders that would address it
(`jarvis wo create <project> "..." --parent {fo_id}`) and submit once they land."""

#: The two ways the handoff gives up and asks the user instead
FEATURE_MANAGER_SILENT = (
    "its review was rejected in round {n} and its manager filed no work orders in "
    "answer, so nothing will resubmit it — decide what has to change, or close it")
FEATURE_MANAGER_STALLED = (
    "its manager was told the work orders it filed after round {n} had landed and did "
    "not resubmit the feature — `jarvis fo submit {fo_id}` is what it was asked to run")

#: And the third, which is not a manager that did nothing but a manager that is not there
FEATURE_MANAGER_MISSING = (
    "it has no project manager work order at all, so nothing will ever resubmit it — a "
    "replacement would be briefed on none of this feature's history; decide what has to "
    "change, or close it (`jarvis fo cancel {fo_id}`)")

#: `kind` of the event that records what the handoff did, and the only memory it has
FEATURE_HANDOFF_EVENT = "manager_handoff"

#: How many transport outages in a row one round survives before the OS gives up on it
VALIDATION_OUTAGE_LIMIT = 3

#: `cause` on the `validation_failed` event of a round whose seats were KILLED BY A SIGNAL (docs/superpowers/specs/2026-10-14-a-killed-validation-panel-opens-an-investigation.md)
VALIDATION_KILLED_CAUSE = "killed"

#: The rc off one seat's recorded reply, for the investigation's per-seat line
_SEAT_RC_RE = re.compile(r"rc=(-?\d+)")


def _seat_rc(reply: str, fallback: int) -> int:
    match = _SEAT_RC_RE.search(reply)
    return int(match.group(1)) if match else fallback


def _moment(ts: float) -> str:
    """A wall-clock moment to the SECOND"""
    return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(ts)) if ts else "unknown"

#: How long to wait before asking GitHub again whether the checks have finished
CI_HOLD_RECHECK_SECONDS = 60.0

#: How long the OS waits for CI in total before judging anyway
CI_HOLD_DEADLINE_SECONDS = 45.0 * 60.0

#: How long a round whose seats could not authenticate waits before going again: 1m (docs/superpowers/specs/2026-09-26-the-panel-must-not-mistake-an-auth-failure-for-a-verdict.md §4)
AUTH_HOLD_BACKOFF = (60.0, 300.0, 900.0, 3600.0)

#: Why a round closed with no verdict at all
NO_VALIDATOR_REASON = (
    "no validator was configured, so this round was never judged — the work order "
    "settled exactly where it settles with validation switched off"
)

#: The pair that records a landing the round machine could not take because a worker turn was in... (docs/superpowers/specs/2026-09-25-a-cap-hold-must-say-so.md)
VALIDATION_LANDING_DEFERRED = "validation_landing_deferred"
VALIDATION_LANDED = "validation_landed"

#: The give-up notification, for both round machines (issue #199)
VALIDATION_ESCALATED_TITLE = "{unit} — the review gave up in round {n}"

#: How much of the reason the body carries
VALIDATION_REASON_CHARS = 500

#: Appended when the cut above actually bit, so a reader can tell a short reason from a clipped one
VALIDATION_REASON_CUT = " […]"


#: What the inbox row for an aggregate re-write-tax alarm says
REWRITE_INBOX_TITLE = {
    inspection.REWRITE_PREFIX_ALARM:
        "{project} is paying to re-send conversations whose prompt PREFIX moved",
    inspection.REWRITE_TTL_ALARM:
        "{project} is paying to re-send conversations whose cache entry EXPIRED",
}

#: The same, for the ONE-HOUR CACHE pair
CACHE_1H_INBOX_TITLE = {
    inspection.CACHE_1H_DISPATCHED_ALARM:
        "Jarvis's OWN turns are buying the one-hour cache write — a transport defect",
    inspection.CACHE_1H_FOREIGN_ALARM:
        "Hand-opened Claude sessions are buying the one-hour cache write",
    inspection.CACHE_ARM_MISSED_ALARM:
        "A cache-TTL arm is not reaching the CLI — stamped turns paid the other rate",
}


def archived_projects(central: CentralStore) -> frozenset[str]:
    """Projects the fleet must not act on (docs/superpowers/specs/2026-10-28-offboard-a-project.md §6)"""
    return frozenset(p["name"] for p in central.list_projects() if p["status"] == ARCHIVED)


def _holds_not_recorded(early: bool) -> tuple[str, ...]:
    """Which `autoreview` holds this PASS must not write to the timeline"""
    from . import autoreview

    shared = (autoreview.HELD_DISABLED, autoreview.HELD_SETTLED, autoreview.HELD_ASKED)
    if early:
        return shared + (autoreview.HELD_JUDGED,)
    return shared + (autoreview.HELD_STATUS, autoreview.HELD_CONFIRMING)


def _hold_is_news(store: ProjectStore, wo_id: str, kind: str,
                  key: tuple[Any, ...],
                  key_of: Callable[[dict[str, Any]], tuple[Any, ...]]) -> bool:
    """Is this hold different from the NEWEST hold of `kind` about the same subject? (#782)"""
    from . import db

    newest: tuple[Any, ...] | None = None
    for event in store.events_of_kind(wo_id, kind):
        candidate = key_of(db.from_json(event["payload"], {}))
        if candidate[0] == key[0]:
            newest = candidate
    return newest != key


def escalation_body(reason: str) -> str:
    """The reason as a notification body — see `VALIDATION_REASON_CHARS`"""
    if len(reason) <= VALIDATION_REASON_CHARS:
        return reason
    return reason[:VALIDATION_REASON_CHARS] + VALIDATION_REASON_CUT


def _landing_deferred(store: ProjectStore, wo_id: str) -> int | None:
    """The round whose landing is still owed — the newest deferral no landing answered"""
    last = store.last_event_of_kind(wo_id, VALIDATION_LANDING_DEFERRED)
    if last is None:
        return None
    owed = (db.from_json(last.get("payload"), {}) or {}).get("round_id")
    if owed is None:
        return None
    landed = store.last_event_of_kind(wo_id, VALIDATION_LANDED)
    done = (db.from_json(landed.get("payload"), {}) or {}).get("round_id") if landed \
        else None
    return None if done == owed else int(owed)


@dataclass(frozen=True)
class CarryOutcome:
    """What `Daemon._carry_catch_up` learned, which is three states and not two (docs/superpowers/specs/2026-09-27-a-conflict-resolution-the-os-asked-for-costs-no-round.md §4.2)"""

    carried: bool
    rebind: bool


class Daemon:
    def __init__(self, catalog: Catalog, poll_interval: float = 5.0):
        self.catalog = catalog
        # Daemon boot arms the transport backstop too, for the catalog it was HANDED (docs/superpowers/specs/2026-09-26-bounded-model-inputs.md)
        claude_cli.set_max_os_prompt_chars(catalog.os.max_os_prompt_chars)
        self.poll_interval = poll_interval
        self.central = CentralStore()
        self.stores: dict[str, ProjectStore] = {}
        self.stop_requested = False
        self.tick_count = 0
        #: Tick each project was last swept for stuck orders on
        self.stuck_swept: dict[str, int] = {}
        #: SORT KEY — `(created_at, id)` — of the last order each project's rules pass actually evaluated (docs/superpowers/specs/2026-09-27-self-evolution.md)
        self.rules_cursor: dict[str, tuple[float, str]] = {}
        # Neo drains its queue on ONE thread: answering in FIFO order back-to-back keeps the shared...
        self.neo_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="neo")
        self.neo_draining = False
        # Dashboard digests run on their OWN thread, not Neo's
        self.digest_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="digest")
        self.digesting = False
        # Morpheus gets its OWN thread: its scripts and its session turns block for up to minutes
        self.morpheus_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="morpheus")
        self.morpheus_busy = False
        # The supervisor gets its OWN thread, and NOT Neo's
        self.supervisor_pool = ThreadPoolExecutor(max_workers=1,
                                                  thread_name_prefix="supervisor")
        self.supervisor_draining = False
        # THE SWEEP GETS ITS OWN POOL AND ITS OWN GUARD, and not the supervisor's (docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md)
        self.health_pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="health")
        self.health_sweeping = False
        # Applying a remedy shares the SUPERVISOR's pool rather than taking a third — §5
        self.remedy_applying = False
        # Validation runs off the tick thread for the same reason Neo does
        self.validate_pool = ThreadPoolExecutor(max_workers=1,
                                                thread_name_prefix="validate")
        # Work orders whose round is in flight
        self.validating: set[str] = set()
        # The validator seam (see `_validator`)
        self.validator: Validator | None = None
        # Orders a reclaim sweep decided to keep: the next sweep spends no git call on them
        self.worktrees_kept: set[str] = set()
        # Projects already warned that their pull requests cannot be polled (no `gh`
        self.pr_poll_warned: set[str] = set()
        self.issue_sync_warned: set[str] = set()
        # The systemd seam for staged releases (src/jarvis/release.py)
        self.release_runner: Any = None
        # The usage meter's `urlopen` seam
        self.meter_opener: Any = None
        # What the catalog file looked like when this catalog was loaded
        self._catalog_stamp = self._catalog_file_stamp()
        # Reload refused (unreadable, unparseable
        self.catalog_reload_warned = False
        #: Last archived set read; kept when a read fails, so nothing evicted comes back (§6.1)
        self.offboarded: frozenset[str] = frozenset()
        self.owner_move_warned = False
        self.evict_offboarded()

    # -- lifecycle

    def store_for(self, project: ProjectSpec) -> ProjectStore:
        if project.name not in self.stores:
            self.stores[project.name] = ProjectStore(project.path)
        return self.stores[project.name]

    def _read_offboarded(self) -> frozenset[str]:
        try:
            self.offboarded = archived_projects(self.central)
        except Exception:  # noqa: BLE001 — fail closed, like `fleet.load_pause`
            log.exception("could not read archived projects; keeping %s", sorted(self.offboarded))
        return self.offboarded

    def evict_offboarded(self) -> None:
        """Drop archived projects from the live catalog and close stores it no longer names (§6.1, §6.2.4)"""
        gone = self._read_offboarded()
        if any(p.name in gone for p in self.catalog.projects):
            owner = self._os_owner()
            self.catalog = self.catalog.without_projects(gone)
            log.info("evicted offboarded project(s) %s", sorted(gone))
            self._warn_owner_moved(owner)
        self._close_orphan_stores()

    def _warn_owner_moved(self, old: str | None) -> None:
        """An offboard that moves fleet-wide OS job ownership says so, once per daemon run (§6.2.5)"""
        new = self._os_owner()
        if old is None or new == old or any(p.name == old for p in self.catalog.projects):
            return
        log.warning("OS job owner moved from %s to %s", old, new)
        if self.owner_move_warned:
            return
        self.owner_move_warned = True
        self.central.add_inbox(
            project="os", level="warning",
            title="fleet-wide OS jobs moved to another project",
            body=(f"{old} owned the fleet-wide OS jobs by fallback (no project's checkout "
                  f"holds the running code) and was offboarded. They now run under "
                  f"{new or 'no project'}."))

    def _close_orphan_stores(self) -> None:
        live = {p.name for p in self.catalog.projects}
        for name in [n for n in self.stores if n not in live]:
            try:
                self.stores.pop(name).close()
            except Exception:  # noqa: BLE001 — a purged file may already be gone
                log.warning("closing %s's store failed", name, exc_info=True)

    def _fleet_stores(self) -> dict[str, ProjectStore]:
        """Every live project's store, for a question about the account (`fleet.read`)"""
        return {p.name: self.store_for(p)
                for p in self.catalog.projects if p.path.is_dir()}

    def steward_digest(self) -> None:
        """The steward's weekly inbox digest (spec 2026-10-21 §3.2 decision 2)"""
        try:
            if steward.digest_due(self.central):
                steward.send_digest(self.catalog, self.central)
        except Exception:  # noqa: BLE001
            log.exception("steward digest failed")

    def offer_findings_to_neo(self, project: ProjectSpec) -> None:
        """Findings Neo was never asked about go to Neo, once each (spec 2026-09-23 §5.5)"""
        from . import ops
        if fleet.load_pause(self.central) is not None:
            # THE BRAKE: a Neo question is spend
            return
        try:
            offered = ops.offer_unasked_findings(project.name, project.path)
        except Exception:  # noqa: BLE001
            log.exception("[%s] offering findings to Neo failed", project.name)
            return
        if offered:
            log.info("[%s] findings offered to Neo: %s", project.name, ", ".join(offered))

    def steward_drain(self) -> None:
        """Queued self-work is deferred, not dropped: re-offer it when there is room (#1181)"""
        if fleet.load_pause(self.central) is not None:
            # THE BRAKE holds new self-work too; settling a queue that can never move is not
            try:
                steward.settle_unrecoverable(self.central)
            except Exception:  # noqa: BLE001
                log.exception("steward: settling unrecoverable queues failed")
            return
        try:
            out = steward.drain(self.catalog, self.central)
        except Exception:  # noqa: BLE001
            log.exception("steward drain failed")
            return
        if out["dispatched"] or out["expired"] or out["failed"]:
            log.info("steward queue: %(dispatched)d re-offered, %(expired)d expired, "
                     "%(failed)d failed, %(waiting)d still waiting", out)

    # -- the account's usage meter

    def sample_usage_meter(self) -> None:
        """One reading of the account's meter, or one gap row saying why there is none"""
        try:
            sample = usage_meter.sample_once(self.central, opener=self.meter_opener)
        except Exception as e:  # noqa: BLE001 — never the token, never the body
            log.warning("usage meter sampler failed: %s", type(e).__name__)
            try:
                usage_meter.record(self.central, usage_meter.Sample(
                    ts=db.now(), ok=False,
                    reason=f"the sampler itself failed ({type(e).__name__})"))
                self.central.conn.commit()
            except Exception:  # noqa: BLE001 — a database that cannot be written is next
                log.exception("usage meter gap row could not be recorded")
            return
        if not sample.ok:
            log.warning("usage meter unreadable: %s", sample.reason)
        self.notice_meter_gap()

    def notice_meter_gap(self) -> None:
        """One `warning` inbox row per failure STREAK, marked in `os_state` (§8)"""
        cfg = self.catalog.os.cost
        streak = usage_meter.gap_streak(self.central)
        marked = self.central.get_state(usage_meter.NOTICE_STREAK_KEY) or ""
        if streak.gap_rows == 0:
            if marked:
                self.central.set_state(usage_meter.NOTICE_STREAK_KEY, "")
                self.central.conn.commit()
            return
        if streak.gap_rows < cfg.meter_gap_notice_samples:
            return
        if marked == str(streak.first_ts):
            return
        minutes = int(streak.gap_rows * usage_meter.SAMPLE_SECONDS / 60)
        self.central.add_inbox(
            "", "the account's usage meter cannot be read",
            f"{streak.gap_rows} consecutive failed reads (~{minutes} minutes): "
            f"{streak.reason}. Every cost surface loses its second, independent number "
            f"until this is fixed — the measured spend has nothing to reconcile against.",
            level="warning")
        self.central.set_state(usage_meter.NOTICE_STREAK_KEY, str(streak.first_ts))
        self.central.conn.commit()

    # -- configuration reload

    def _catalog_file_stamp(self) -> tuple[int, str] | None:
        """`(mtime_ns, sha256)` of the catalog file, or None when there is nothing on disk to watch"""
        path = self.catalog.source_path
        if path is None:
            return None
        try:
            return (path.stat().st_mtime_ns, hashlib.sha256(path.read_bytes()).hexdigest())
        except OSError:
            return None

    def reload_catalog(self) -> bool:
        """Pick up a config change without a restart, once per tick"""
        path = self.catalog.source_path
        if path is None:
            return False
        try:
            mtime = path.stat().st_mtime_ns
        except OSError as e:
            self._refuse_reload(f"cannot read the catalog at {path}: {e}")
            return False
        if self._catalog_stamp is not None and mtime == self._catalog_stamp[0]:
            return False
        try:
            digest = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError as e:
            self._refuse_reload(f"cannot read the catalog at {path}: {e}")
            return False
        seen, self._catalog_stamp = self._catalog_stamp, (mtime, digest)
        if seen is not None and digest == seen[1]:
            return False
        try:
            fresh = load_catalog(path)
        except Exception as e:  # noqa: BLE001 — one bad file must not stop the fleet
            self._refuse_reload(
                f"{e}\n\nThe fleet is still running the configuration it started with. "
                f"Fix {path} and it will be picked up on the next tick.")
            return False
        # Removing an archived or purged (row-less) project is applicable; any other set change is not (§6.1)
        before = {p.name for p in self.catalog.projects}
        try:
            registered = {p["name"] for p in self.central.list_projects()}
        except Exception:  # noqa: BLE001 — unreadable registry: no removal counts as purged
            registered = before
        after = {p.name for p in fresh.projects}
        gone = self._read_offboarded()
        offboarded = {n for n in before - after if n in gone or n not in registered}
        if after - gone != before - gone - offboarded:
            self._refuse_reload(
                f"{path} added or removed a project, and that is not a setting: a new "
                f"project has never been through `bootstrap_project`, and a removed one "
                f"leaves an open store behind. Run `jarvis start` to apply it. Setting "
                f"changes in the same file are not applied either until you do.")
            return False
        owner = self._os_owner()
        self.catalog = fresh.without_projects(gone)
        self._warn_owner_moved(owner)
        self._close_orphan_stores()
        self.catalog_reload_warned = False
        log.info("catalog reloaded from %s", path)
        return True

    def rebase_config_for_release(self) -> dict[str, Any] | None:
        """Re-resolve the head version under the running build, once, at daemon start"""
        from . import bugreport, config_version
        from .catalog import CatalogError, parse_catalog

        head = self.central.head_config_version()
        if head is None:
            return None
        build = bugreport.jarvis_version()
        try:
            resolved = config_version.resolve(parse_catalog(head["document"]))
        except CatalogError as e:
            # A historical document this build can no longer parse
            log.warning("config rebase skipped: head %s does not parse under %s (%s)",
                        head["id"], build, e)
            return None
        moved = config_version.diff(head["resolved"], resolved)
        if not moved:
            return None
        # Changed settings before the defaults this release added (issue 1235)
        first = sorted(moved, key=lambda c: c.get("kind") == "added")
        shown = "; ".join(f"{c['path']} {c['old']!r} → {c['new']!r}" for c in first[:5])
        more = f" (and {len(moved) - 5} more)" if len(moved) > 5 else ""
        row = self.central.add_config_version(
            head["document"], resolved, actor="release",
            reason=f"upgrade {head['schema_version']} → {build}: {shown}{more}",
            changes=moved, source_path=head["source_path"], schema_version=build)
        log.info("config rebased for %s: %d default(s) moved (%s)",
                 build, len(moved), row["id"])
        return row

    def _refuse_reload(self, detail: str) -> None:
        """Once per daemon run, not once per tick"""
        log.warning("catalog reload refused: %s", detail)
        if self.catalog_reload_warned:
            return
        self.catalog_reload_warned = True
        self.central.add_inbox(
            project="os", level="warning",
            title="a catalog change was NOT applied", body=detail)

    def run_forever(self) -> None:
        ensure_home()
        self._write_pidfile()
        signal.signal(signal.SIGTERM, self._on_signal)
        signal.signal(signal.SIGINT, self._on_signal)
        log.info("jarvisd started (pid=%s, projects=%s)",
                 os.getpid(), [p.name for p in self.catalog.projects])
        # Before the first tick, and before anything reads a version: this boot may be the first under a new...
        self.rebase_config_for_release()
        # Before the first tick: if the boot we are living through IS a staged release's restart
        self.verify_pending_release()
        try:
            invariants_mod.report_at_startup("daemon")
        except Exception:  # noqa: BLE001 — a startup check must not stop the daemon
            log.exception("startup invariant check failed")
        try:
            while not self.stop_requested:
                started = time.monotonic()
                try:
                    self.tick()
                except Exception:  # noqa: BLE001 — the loop must survive anything
                    log.exception("tick failed")
                elapsed = time.monotonic() - started
                time.sleep(max(0.2, self.poll_interval - elapsed))
        finally:
            self.neo_pool.shutdown(wait=False)
            self.digest_pool.shutdown(wait=False)
            self.morpheus_pool.shutdown(wait=False)
            self._remove_pidfile()
            log.info("jarvisd stopped")

    def _on_signal(self, signum: int, frame: object) -> None:
        log.info("received signal %s, shutting down", signum)
        self.stop_requested = True

    def _write_pidfile(self) -> None:
        daemon_pidfile().write_text(str(os.getpid()))
        self.central.set_state("daemon_pid", str(os.getpid()))
        self.central.set_state("daemon_started_at", str(time.time()))
        if self.catalog.source_path:
            self.central.set_state("catalog_path", str(self.catalog.source_path))

    def _remove_pidfile(self) -> None:
        daemon_pidfile().unlink(missing_ok=True)

    # -- main tick

    def tick(self) -> None:
        # First, so everything below runs under one configuration — and the same one
        self.reload_catalog()
        self.evict_offboarded()
        self.tick_count += 1
        reconcile = self.tick_count % RECONCILE_EVERY_TICKS == 1
        poll_prs = self.tick_count % PR_POLL_EVERY_TICKS == 1
        retry_paused = self.tick_count % RETRY_EVERY_TICKS == 1
        sweep_landings = self.tick_count % LANDING_SWEEP_EVERY_TICKS == 1
        run_schedule = self.tick_count % SCHEDULE_EVERY_TICKS == 1
        scan_cache_ttl = \
            self.tick_count % CACHE_TTL_EVERY_TICKS == CACHE_TTL_TICK_OFFSET
        # The same offset and for the same reason: this walks transcripts too
        reconcile_meter = \
            self.tick_count % METER_RECONCILE_EVERY_TICKS == CACHE_TTL_TICK_OFFSET
        # `None` means "the roster was not read this tick"
        sessions_by_project: dict[str, list[claude_cli.BgSession]] | None = None
        # The ACCOUNT's state, read ONCE for the whole tick and then spent down as turns launch
        state = fleet.read(self.catalog.os.max_in_flight, self._fleet_stores())
        if fleet.announce(self.central, state):
            log.warning("fleet held: %s", state.blocked())
        # AFTER `announce`, which is what records a reopen: the ramp it starts has to be in force on the very... (#843)
        fleet.attach(state, self.central)
        # OUTSIDE the project loop, beside `fleet.read`: the meter is an ACCOUNT fact
        if self.tick_count % USAGE_SAMPLE_EVERY_TICKS == 1:
            self.sample_usage_meter()
            self.steward_digest()
        if self.tick_count % STEWARD_DRAIN_EVERY_TICKS == 1:
            self.steward_drain()
        # The roster is a subprocess, and tracking injected sessions is the only thing left that reads it
        if reconcile and self._tracking_injected_sessions():
            try:
                sessions_by_project = {}
                for s in claude_cli.list_background_sessions():
                    sessions_by_project.setdefault(s.cwd, []).append(s)
            except claude_cli.ClaudeCliError as e:
                log.warning("agents listing failed: %s", e)
                sessions_by_project = None

        for project in self.catalog.projects:
            if not project.path.is_dir():
                continue
            store = self.store_for(project)
            try:
                self.route_outbox(project, store)
                # Turns are Jarvis's own processes, so reaping them costs a signal and a file read
                self.settle_turns(project, store)
                # Before delivery, not after: a work order whose window has just reopened must re-send the turn it...
                if retry_paused:
                    self.retry_paused_turns(project, store, state)
                self.resume_budget_held(project, store, state)
                # Before delivery, not after: an envelope BECOMES a queued message
                self.deliver_envelopes(project, store)
                self.deliver_messages(project, store, state)
                # After delivery, not before: an envelope this round machine posted on an earlier tick is already on...
                self.validation_tick(project, store, state)
                # Beside its twin and for the same reason: an envelope a feature round posted on an earlier tick has...
                self.feature_validation_tick(project, store, state)
                # Before dispatch, not after: a planner filed this tick is an ordinary pending work order
                self.plan_features(project, store)
                # Immediately after: this is the tick's feature-order planning band
                self.refresh_plan_specs(project, store)
                # Before dispatch for the planner's reason one step removed: an order the scheduler files this tick...
                if run_schedule:
                    self.schedule_tick(project, store)
                # Its own cadence, and OUTSIDE the reconcile block: a ~20s transcript walk (`CACHE_TTL_EVERY_TICKS`)...
                if scan_cache_ttl:
                    self.check_cache_ttl(project, store)
                # Its own cadence, and the OS-owning project only
                if reconcile_meter:
                    self.check_meter_residual(project, store)
                # IMMEDIATELY BEFORE dispatch, not after: `sync_issues` files a release order late in one tick and...
                if reconcile:
                    self.clear_settled_preconditions(project, store)
                self.hold_red_release(project, store)
                self.dispatch_pending(project, store, state)
                if poll_prs:
                    # BEFORE the pull-request poll, so the same tick's `auto_merge` decisions read a fresh fact rather...
                    self.poll_default_branch(project, store)
                    # BETWEEN the two, spec 2026-10-14-a-red-main-dispatches-its-own-fix.md §2
                    self.dispatch_base_fix(project, store)
                    self.poll_pull_requests(project, store)
                    # AFTER the poll: a pushed-branch park has no `pr_url` for it to read (2026-10-12 spec §9)
                    self.attach_pull_requests(project, store)
                    # AFTER the poll, in the same tick: a merge that just completed a work order is what closes its... (#240)
                    self.sync_issues(project, store)
                    # Beside it, on the same cadence and for the same reason: the tracker is the surface a person scans...
                    self.sync_issue_references(project, store)
                    # After `sync_issues`, which is what files release orders
                    self.settle_shipped_releases(project, store)
                    # Immediately after it, never before: issue #945 spec §2.1
                    self.refile_dropped_fixes(project, store)
                # After the pull-request poll, so the merge that completes a feature's last child settles the feature...
                self.settle_features(project, store)
                if reconcile:
                    # Immediately after the settlement above
                    self.clear_answered_investigations(project, store)
                    # Beside it: the investigator's half of a settled investigation
                    self.settle_landed_investigators(project, store)
                    # A report filed before Neo decided findings still owes Neo its questions
                    self.offer_findings_to_neo(project)
                    # The agents roster holds ONLY the user's own sessions: workers are headless and never enter it
                    if sessions_by_project is not None:
                        self.track_injected_sessions(project, store, sessions_by_project)
                    # After settlement, so an order that finished this tick is billed on this tick
                    self.seal_bills(project, store)
                    # Immediately after it: the same argument, about the same evidence
                    self.seal_autopsies(project, store)
                    # Before the invariants, because it is a fact about work that is still RUNNING rather than about...
                    self.check_burning_turns(project, store)
                    # AFTER `seal_bills`, never before: it reads the sealed bills
                    self.check_rewrite_tax(project, store)
                    # Also before them: this is the only thing that ever closes a gate request no reviewer can see
                    self.abandon_unargued_gates(project, store)
                    # On the reconcile cadence rather than every tick
                    self.auto_review(project, store)
                    # Beside it, and AFTER it: the same tick that files an objection is the one that must take it back if...
                    self.withdraw_stale_objections(project, store)
                    # After the withdrawals it counts, so a withdrawal this tick is in the rate
                    self.check_objection_withdrawals(project, store)
                    # Immediately before the check that reads it
                    if sweep_landings:
                        self.refresh_landings(project, store)
                    # After settlement, so an order completed on this tick starts its grace period here
                    self.reclaim_worktrees(project, store)
                    # Last: check the state everything above just produced
                    self.check_invariants(project, store, sweep_landings=sweep_landings)
                    # AFTER the invariants, and the position is the whole argument... (docs/superpowers/specs/2026-09-27-self-evolution.md §5.1)
                    self.rules_tick(project, store)
                self.central.touch_project(project.name)
            except Exception:  # noqa: BLE001
                log.exception("project %s tick failed", project.name)

        # After the project loop, so `running_turns` reflects the turns settlement just reaped: the...
        self.release_tick()

        self.neo_tick()
        # After the drain is kicked, never before: a question digested this tick is one whose answer has...
        self.digest_tick()
        # Off its own pool, on its own cadence: production validation (docs/superpowers/specs/2026-10-21-morpheus-validates-releases-in-production.md)
        self.morpheus_tick()
        # After the project loop, so an alarm `check_burning_turns` raised on this tick is judged on this...
        self.supervisor_tick()
        # AFTER the supervisor's kick, never before: both run off their own pools
        self.health_tick(state)
        # Immediately after it, sharing that reading
        if self.tick_count % self.stuck_cadence() == 1:
            due = self.stuck_due_projects()
            if due:
                self.stuck_tick(state, projects=due)
        # Last of the three, and never merged into the review: a proposal filed above cannot be applied on...
        self.remedy_tick()

        # Before routing: a dashboard failure raised here goes out with this tick's notifications instead of...
        try:
            self.check_ui_log()
        except Exception:  # noqa: BLE001 — never let the UI watch stall the tick
            log.exception("ui log check failed")
        # Beside it, under the same discipline: a wedge found this tick is notified on this tick
        try:
            self.check_ui_health()
        except Exception:  # noqa: BLE001 — same rule
            log.exception("ui health check failed")
        # AFTER the reclaim sweeps above, which are what the item would otherwise ask the user to do
        if reconcile:
            try:
                self.check_disk_space()
            except Exception:  # noqa: BLE001 — same rule
                log.exception("disk space check failed")

        from .notify import route_new_inbox
        route_new_inbox(self.central, self.catalog)

        # LAST, so a reconcile pass that hangs goes dark instead of reporting itself alive (docs/superpowers/specs/2026-10-16-a-stopped-daemon-is-a-liveness-gap.md)
        if reconcile:
            self.central.set_state(RECONCILE_TICK_KEY, db.to_json(
                {"ts": db.now(),
                 "interval": RECONCILE_EVERY_TICKS * self.poll_interval}))

    def seal_bills(self, project: ProjectSpec, store: ProjectStore) -> None:
        """Freeze the bill of every order that has settled and has none yet"""
        from . import bill as bill_mod, db, usage

        pending = store.unsealed_terminal_orders()
        features = store.unsealed_terminal_features()
        if not pending and not features:
            return
        # One index of Claude Code's transcript tree for the whole batch: it walks every project directory...
        index = usage.index_sessions()
        for order in pending:
            try:
                bill_mod.seal(project.name, project.path, order, index=index)
            except Exception:  # noqa: BLE001 — a bill must never stall the tick
                log.exception("sealing the bill for %s failed", order["id"])
                # Sealed EMPTY rather than left pending
                store.seal_bill(order["id"], db.to_json(
                    {"error": "this bill could not be computed when the order settled"}))
        for feature in features:
            try:
                bill_mod.seal(project.name, project.path, feature,
                              feature=True, index=index)
            except Exception:  # noqa: BLE001
                log.exception("sealing the bill for %s failed", feature["id"])
                store.seal_bill(feature["id"], db.to_json(
                    {"error": "this bill could not be computed when the order settled"}),
                    feature=True)

    def seal_autopsies(self, project: ProjectSpec, store: ProjectStore) -> None:
        """Freeze the ANATOMY of every settled order that has none yet (docs/superpowers/specs/2026-09-27-order-autopsy-durability.md)"""
        from . import autopsy as autopsy_mod, db, observability, usage

        resolved = [(order, observability.level_for(order, project.observability))
                    for order in store.unsealed_autopsy_orders()]
        pending = [pair for pair in resolved if pair[1] != observability.OFF]
        if pending:
            index = usage.index_sessions()
            for order, level in pending:
                try:
                    autopsy_mod.seal(project.name, project.path, order, index=index,
                                     level=level)
                except Exception:  # noqa: BLE001 — an autopsy must never stall the tick
                    log.exception("sealing the autopsy for %s failed", order["id"])
                    store.seal_autopsy(order["id"], db.to_json(
                        {"error":
                         "this autopsy could not be read when the order settled"}))
        # No second `usage.index_sessions()` is hoisted for the queue below: an upgrade re-derives from the...
        for order in store.stale_autopsy_orders(autopsy_mod.PAYLOAD_VERSION):
            if not autopsy_mod.records_autopsy(order, project.observability):
                continue
            sealed = autopsy_mod.unseal(order)
            if sealed is None:  # an ERROR payload: there is nothing to re-derive from
                continue
            # None back means the old seal stands — not a failure, and nothing to retry
            autopsy_mod._upgrade_seal(project.name, project.path, order, sealed)

    # -- 4. dispatch

    def dispatch_pending(self, project: ProjectSpec, store: ProjectStore,
                         state: fleet.Fleet | None = None) -> None:
        """Claim and launch what this project may run — under BOTH caps"""
        # The user's pause is per order, so it narrows the CLAIM rather than stopping it: only allow-listed... (#843)
        only = (state.pause.allow if state is not None and state.pause is not None
                else None)
        while store.count_active() < project.max_concurrent:
            if state is not None and (state.shut() or state.capacity_cause()):
                return
            wo = store.claim_next_pending(only=only)
            if wo is None:
                return
            log.info("[%s] dispatching %s: %s", project.name, wo["id"], wo["title"])
            try:
                dispatch_work_order(
                    store, self.central, project, wo, os_config=self.catalog.os,
                )
            except claude_cli.ClaudeCliError as e:
                log.error("[%s] dispatch of %s failed: %s", project.name, wo["id"], e)
                continue
            if state is not None:
                state.launched()

    # -- staged releases (thin hooks; all logic in src/jarvis/release.py)

    def _release_store(self, project_name: str) -> ProjectStore | None:
        """The marker names its project; this is how release.py reaches its store"""
        for p in self.catalog.projects:
            if p.name == project_name and p.path.is_dir():
                return self.store_for(p)
        return None

    def verify_pending_release(self) -> None:
        from . import release

        try:
            release.verify_on_boot(self._release_store, runner=self.release_runner)
        except Exception:  # noqa: BLE001 — a broken marker must not stop the daemon
            log.exception("release boot verification failed")

    @property
    def self_dispatch(self) -> bool:
        # os.autonomy.self_dispatch: may the OS create orders nobody typed?
        return bool(self.catalog.os.autonomy.self_dispatch)

    def release_tick(self) -> None:
        from . import release

        try:
            release.maybe_restart(self._release_store, runner=self.release_runner)
        except Exception:  # noqa: BLE001
            log.exception("release restart check failed")

    # -- 4a. feature orders: open a planner

    def plan_features(self, project: ProjectSpec, store: ProjectStore) -> None:
        """Give every unplanned feature order its planner (docs/superpowers/specs/2026-09-27-investigation-orders.md)"""
        for fo in store.list_feature_orders(statuses=("pending",)):
            try:
                wo = store.create_work_order(
                    title=f"Plan: {fo['title']}"[:200],
                    # The ask verbatim
                    description=fo["description"],
                    origin="jarvis", kind="planner", parent_id=fo["id"],
                )
            except Exception:  # noqa: BLE001 — one bad feature order must not stop the rest
                log.exception("[%s] could not open a planner for %s", project.name,
                              fo["id"])
                continue
            store.update_feature_order(fo["id"], plan_wo_id=wo["id"])
            store.set_feature_status(fo["id"], "planning")
            log.info("[%s] planning %s: opened %s", project.name, fo["id"], wo["id"])

        for fo in store.list_feature_orders(statuses=("pending",), kind="improvement"):
            try:
                wo = store.create_work_order(
                    title=f"Analyse: {fo['title']}"[:200],
                    # The observation verbatim — the analyst CONTRACT is composed at dispatch (dispatch._analyst_prompt)
                    description=fo["description"],
                    origin="jarvis", kind="analyst", parent_id=fo["id"],
                )
            except Exception:  # noqa: BLE001 — one bad improvement order must not stop the rest
                log.exception("[%s] could not open an analyst for %s", project.name,
                              fo["id"])
                continue
            store.update_feature_order(fo["id"], plan_wo_id=wo["id"])
            store.set_feature_status(fo["id"], "planning")
            log.info("[%s] analysing %s: opened %s", project.name, fo["id"], wo["id"])

        # §2.7 of docs/superpowers/specs/2026-09-27-investigation-orders.md: a third sibling loop
        for fo in store.list_feature_orders(statuses=("pending",),
                                            kind="investigation"):
            try:
                wo = store.create_work_order(
                    title=f"Investigate: {fo['title']}"[:200],
                    # The `why` verbatim; the investigator CONTRACT is composed at dispatch...
                    description=fo["description"],
                    origin="jarvis", kind="investigator", parent_id=fo["id"],
                )
            except Exception:  # noqa: BLE001 — one bad investigation must not stop the rest
                log.exception("[%s] could not open an investigator for %s", project.name,
                              fo["id"])
                continue
            store.update_feature_order(fo["id"], plan_wo_id=wo["id"])
            # LAST, which is what makes a crashed tick re-file rather than strand an investigation with no...
            store.set_feature_status(fo["id"], "planning")
            log.info("[%s] investigating %s: opened %s", project.name, fo["id"],
                     wo["id"])

    def refresh_plan_specs(self, project: ProjectSpec, store: ProjectStore) -> None:
        """Re-ask any plan review whose spec has been committed over"""
        from . import ops

        for fo in store.list_feature_orders(statuses=("plan_review",)):
            try:
                ops.refresh_plan_spec(fo["id"], project_name=project.name)
            except Exception:  # noqa: BLE001 — one feature must not stop the rest
                log.exception("[%s] could not refresh the spec under review for %s",
                              project.name, fo["id"])

    def settle_features(self, project: ProjectSpec, store: ProjectStore) -> None:
        """Close out feature orders whose children have all landed, or one of which has not (docs/superpowers/specs/2026-08-29-feature-order-resume.md)"""
        from .invariants import (
            FEATURE_CHILD_CANCELLED,
            FEATURE_CHILD_FAILED,
            dead_feature_children,
        )

        # PASS ONE: THE FAMILY BUDGET, EVERY KIND (docs/superpowers/specs/2026-10-01-a-family-capped-raise-must-say-so.md)
        for kind, running in _FAMILY_RUNNING_STATUS.items():
            for fo in store.list_feature_orders(
                    statuses=(running, budget_mod.FO_EXHAUSTED), kind=kind):
                pool = budget_mod.feature_exhaustion(store, self.central, fo)
                if pool is not None:
                    budget_mod.escalate_feature(store, fo, pool)
                    continue
                if fo["status"] == budget_mod.FO_EXHAUSTED:
                    # Topped up: back to work
                    store.set_feature_status(fo["id"], running)
                    store.clear_feature_attention(fo["id"])

        # PASS TWO: THE CHILD SETTLEMENT, FEATURE-ONLY BY LIFECYCLE, `kind='feature'` WRITTEN OUT
        for fo in store.list_feature_orders(statuses=("executing",), kind="feature"):
            children = store.feature_children(fo["id"], kinds=WORKER_CHILDREN)
            if not children:
                continue  # released with nothing in it; nothing to settle against
            # A superseded child settles its feature NEITHER WAY
            live = [c for c in children if not c["superseded"]]
            dead = dead_feature_children(store, children)
            if dead:
                first = dead[0]
                template = (FEATURE_CHILD_FAILED if first["status"] == "failed"
                            else FEATURE_CHILD_CANCELLED)
                reason = template.format(id=first["id"])
                store.set_feature_status(fo["id"], "failed")
                store.flag_feature_attention(fo["id"], reason)
                self._close_feature_manager(store, fo["id"])
                log.info("[%s] feature %s failed: %s", project.name, fo["id"], reason)
            elif all(c["status"] == "completed" for c in live):
                if self._route_to_validation(project, store, fo):
                    continue
                self._complete_feature(store, fo)
                log.info("[%s] feature %s completed (%d work orders)", project.name,
                         fo["id"], len(children))

    def clear_answered_investigations(self, project: ProjectSpec,
                                      store: ProjectStore) -> None:
        """Take down a `WAITING_ON_USER` flag once its subject owes the user nothing"""
        from . import ops

        for fo in store.list_feature_orders(statuses=("completed",),
                                            kind="investigation"):
            if not fo["needs_attention"]:
                continue
            plan = db.from_json(fo.get("plan"), {}) or {}
            if plan.get("classification") != "WAITING_ON_USER":
                continue
            subject = str((db.from_json(fo.get("metadata"), {}) or {}).get(
                ops.SUBJECT_KEY) or "")
            try:
                if not self._subject_is_clear(store, subject):
                    continue
            except Exception:  # noqa: BLE001 — one investigation must not stop the rest
                log.exception("[%s] could not read %s's subject", project.name, fo["id"])
                continue
            store.clear_feature_attention(fo["id"])
            # The investigator carries the record: `ops.feature_event` is manager-only and an investigation has...
            carrier = store.carrier_for_feature(fo["id"])
            if carrier is not None:
                store.add_event(carrier["id"], "investigation_attention_cleared", {
                    "investigation": fo["id"], "subject": subject,
                    "why": f"{subject or 'the subject'} no longer needs the user",
                })
            log.info("[%s] %s: the user has dealt with %s", project.name, fo["id"],
                     subject)

    @staticmethod
    def _subject_is_clear(store: ProjectStore, subject: str) -> bool:
        """Does the investigated order still owe the user anything? §2.5's predicate"""
        if not subject:
            return True
        try:
            if subject.startswith("wo-"):
                return not invariants_mod.true_blockers(store,
                                                        store.get_work_order(subject))
            return not store.get_feature_order(subject)["needs_attention"]
        except KeyError:
            return True   # gone, so nothing is owed on it

    def _route_to_validation(self, project: ProjectSpec, store: ProjectStore,
                             fo: dict) -> bool:
        """Should this finished feature go to the panel instead of to `completed`? (#480)"""
        from . import ops

        cfg = project.validation
        if not (cfg.enabled and cfg.feature_units):
            return False
        if store.validation_rounds(fo_id=fo["id"]):
            # Judged once already; the manager owns the next submission (#480)
            self._manager_handoff(project, store, fo)
            return True
        if self._defer_unintegrated_children(project, store, fo, cfg):
            return True
        try:
            round_row = ops.submit_feature_for_validation(
                store, project.path, fo, declared="", summary="", cfg=cfg)
        except ops.UnintegratedChild as e:
            # §2: never the arm below, which completes the feature unjudged
            log.info("[%s] feature %s: %s", project.name, fo["id"], e)
            self._defer_for(project, store, fo["id"], e.child_id, e.commit, e.head, cfg)
            return True
        except Exception:  # noqa: BLE001 — one feature must not cost the tick the rest
            log.exception("[%s] could not open a validation round for %s",
                          project.name, fo["id"])
            return False
        log.info("[%s] feature %s -> validating (round %d)", project.name, fo["id"],
                 round_row["round"])
        return True

    #: §3: one row per distinct deferral state, on the feature's own timeline
    VALIDATION_DEFER_EVENT = "validation_defer"

    def _defer_unintegrated_children(self, project: ProjectSpec, store: ProjectStore,
                                     fo: dict, cfg: Any) -> bool:
        """Is a merged child's commit still missing from the head a round would judge? (docs/superpowers/specs/2026-10-07-a-feature-round-must-judge-a-head-that-contains-its-children.md)"""
        try:
            return self._unintegrated_child(project, store, fo, cfg)
        except Exception:  # noqa: BLE001 — §2: cannot prove it is integrated, so wait
            log.exception("[%s] could not check %s's children against the default "
                          "branch head", project.name, fo["id"])
            return True

    def _unintegrated_child(self, project: ProjectSpec, store: ProjectStore,
                            fo: dict, cfg: Any) -> bool:
        """`_defer_unintegrated_children` without the catch-all"""
        from . import branchproof, ops
        from . import evidence as evidence_mod

        # §2: the head is read AFTER a fetch, so the check and the collection agree
        ref = evidence_mod.base_ref(project.path)
        if ref:
            branchproof.fetch(project.path, ref)
        head = evidence_mod.default_branch_head(project.path)
        if not head:
            return False  # no default branch: the collector resolves no head either
        missing = ops.unintegrated_children(
            project.path, store, fo["id"], head,
            lambda child: self._merge_commit_of(project, store, child))
        if not missing:
            return False
        child, sha = missing[0]
        self._defer_for(project, store, fo["id"], child["id"], sha, head, cfg)
        return True

    def _defer_for(self, project: ProjectSpec, store: ProjectStore, fo_id: str,
                   child_id: str, sha: str, head: str, cfg: Any) -> None:
        """Record one deferral: the timeline event, and the user on expiry (docs/superpowers/specs/2026-10-07-a-feature-round-must-judge-a-head-that-contains-its-children.md)"""
        from .invariants import FEATURE_CHILD_NOT_INTEGRATED

        waited = self._minutes_since_merge(store, {"id": child_id})
        expired = waited >= int(cfg.feature_merge_wait_minutes)
        self._record_defer(store, fo_id, {
            "child": child_id, "commit": sha, "head": head,
            "waited_minutes": round(waited, 1), "expired": expired})
        if expired:
            # §3: a child merged into a sibling branch never becomes an ancestor
            store.flag_feature_attention(fo_id, FEATURE_CHILD_NOT_INTEGRATED.format(
                id=child_id, commit=sha, head=head))
        log.info("[%s] feature %s: %s's merge %s is not in %s after %.0f min — no round "
                 "opened%s", project.name, fo_id, child_id, sha[:12], head[:12],
                 waited, " (asking the user)" if expired else "")

    def _minutes_since_merge(self, store: ProjectStore, child: dict) -> float:
        """How long ago this child's merge was recorded. §3: minutes, never ticks"""
        for kind in ("pr_merged", self.MERGE_COMMIT_EVENT):
            rows = store.events_of_kind(child["id"], kind)
            if rows:
                return max(0.0, (time.time() - float(rows[-1]["ts"])) / 60)
        return 0.0

    def _record_defer(self, store: ProjectStore, fo_id: str,
                      payload: dict[str, Any]) -> None:
        """One event per distinct `(child"""
        from . import ops

        state = (payload["child"], payload["commit"], payload["expired"])
        prior = ops.feature_events_of_kind(store, fo_id, self.VALIDATION_DEFER_EVENT)
        if prior:
            last = db.from_json(prior[-1]["payload"], {})
            if (last.get("child"), last.get("commit"),
                    bool(last.get("expired"))) == state:
                return
        ops.feature_event(store, fo_id, self.VALIDATION_DEFER_EVENT, payload)

    def _manager_handoff(self, project: ProjectSpec, store: ProjectStore,
                         fo: dict) -> None:
        """Tell an idle manager its children landed — or give up and ask the user (#480)"""
        from . import ops

        fo_id = fo["id"]
        last = store.latest_validation_round(fo_id=fo_id)
        if not last or last["outcome"] in RUNNABLE_VALIDATION_OUTCOMES:
            return  # a round is in flight or being retried: not the manager's turn yet
        manager = store.manager_work_order(fo_id)
        if manager is None:
            # NEVER A SILENT RETURN, and never a replacement manager either: a fresh one would answer this...
            reason = FEATURE_MANAGER_MISSING.format(fo_id=fo_id)
            carrier = store.carrier_for_feature(fo_id)
            said = (store.events_of_kind(carrier["id"], FEATURE_HANDOFF_EVENT)
                    if carrier else [])
            if any(db.from_json(e["payload"], {}).get("reason") == reason
                   for e in said):
                return
            store.flag_feature_attention(fo_id, reason)
            if carrier is not None:
                store.add_event(carrier["id"], FEATURE_HANDOFF_EVENT,
                                {"round": int(last["round"]), "action": "flagged",
                                 "reason": reason, "children": [],
                                 "feature_order": fo_id})
            # No session at all — a feature nobody has planned
            log.info("[%s] feature %s flagged: %s", project.name, fo_id, reason)
            return
        # A manager that is SETTLED is revived and this branch goes on to its nudge or its flag
        if (manager["status"] != "idle"
                and ops.revive_feature_manager(store, fo_id, why="manager handoff")
                is None):
            return
        # ONE reading of "has anybody spoken to it", shared with `ops.stuck_scan`
        if not bus.nothing_queued_to_manager(store, fo_id, manager["id"]):
            return

        n = int(last["round"])
        said = [p for p in (db.from_json(e["payload"], {}) for e in
                            ops.feature_events_of_kind(store, fo_id,
                                                       FEATURE_HANDOFF_EVENT))
                if int(p.get("round") or 0) == n]
        covered = {wo_id for p in said if p.get("action") == "nudged"
                   for wo_id in (p.get("children") or ())}
        landed = tuple(c["id"] for c in store.feature_children(fo_id,
                                                               kinds=WORKER_CHILDREN)
                       if not c["superseded"] and float(c["created_at"]) > last["ts"])
        fresh = tuple(wo_id for wo_id in landed if wo_id not in covered)
        if fresh:
            bus.post(store, subject=bus.Subject(fo_id=fo_id),
                     from_role="reconciler", to_role="manager",
                     payload=bus.ChildrenLanded(
                         round=n, children=fresh,
                         note=FEATURE_CHILDREN_LANDED.format(fo_id=fo_id)))
            ops.feature_event(store, fo_id, FEATURE_HANDOFF_EVENT,
                              {"round": n, "action": "nudged", "children": list(fresh),
                               "feature_order": fo_id})
            log.info("[%s] feature %s: told its manager %d work order(s) landed after "
                     "round %d", project.name, fo_id, len(fresh), n)
            return

        # Nothing new
        here = sorted(covered)
        if any(p.get("action") == "flagged" and sorted(p.get("children") or ()) == here
               for p in said):
            return
        template = FEATURE_MANAGER_STALLED if covered else FEATURE_MANAGER_SILENT
        reason = template.format(n=n, fo_id=fo_id)
        store.flag_feature_attention(fo_id, reason)
        ops.feature_event(store, fo_id, FEATURE_HANDOFF_EVENT,
                          {"round": n, "action": "flagged", "reason": reason,
                           "children": here, "feature_order": fo_id})
        log.info("[%s] feature %s flagged: %s", project.name, fo_id, reason)

    def _complete_feature(self, store: ProjectStore, fo: dict) -> None:
        """The one place a feature order ends successfully"""
        store.set_feature_status(fo["id"], "completed")
        store.clear_feature_attention(fo["id"])
        self._close_feature_manager(store, fo["id"])
        self._close_feature_backlog(fo)

    def _close_feature_manager(self, store: ProjectStore, fo_id: str) -> None:
        """End the project manager order when its feature ends, whichever way it ended"""
        manager = store.manager_work_order(fo_id)
        if not manager or manager["status"] not in OPEN_STATUSES:
            return
        from . import ops as ops_mod

        # RECORDS, like `mark_done`, and does not refuse: the feature is over and parking its manager would...
        work = ops_mod.authorship(store, manager)
        store.set_status(manager["id"], "completed")
        store.clear_attention(manager["id"])
        store.add_event(manager["id"], "feature_settled",
                        {"feature_order": fo_id,
                         **({"authored": work.record()} if work.base else {})})

    def _close_feature_backlog(self, fo: dict) -> None:
        """A feature order promoted from the backlog closes its item when it lands"""
        if not fo.get("backlog_id"):
            return
        try:
            self.central.mark_backlog(fo["backlog_id"], "done")
        except Exception:  # noqa: BLE001 — a stale backlog id must not stop the settle
            log.exception("could not close backlog item %s", fo["backlog_id"])

    # -- 0. the dashboard

    def reclaim_worktrees(self, project: ProjectSpec,
                          store: ProjectStore) -> list[str]:
        """Give back the disk the checkouts of settled orders hold (docs/superpowers/specs/2026-10-22-a-settled-checkout-is-reclaimed.md §1)"""
        from . import disk

        try:
            reclaimed, kept = disk.sweep(store, project, skip=self.worktrees_kept)
        except Exception:  # noqa: BLE001 — housekeeping must never take the tick down
            log.exception("[%s] worktree reclaim failed", project.name)
            return []
        self.worktrees_kept.update(kept)
        if reclaimed:
            log.info("[%s] reclaimed %d settled checkout%s: %s", project.name,
                     len(reclaimed), "s" if len(reclaimed) != 1 else "",
                     ", ".join(reclaimed))
        return reclaimed

    def check_disk_space(self) -> str:
        """One inbox item when a volume holding OS state runs low (same spec, §2)"""
        from . import disk

        floor = self.catalog.os.disk_free_warn_gb
        if floor <= 0:
            return "off"
        volumes = disk.state_volumes(self.catalog)
        if not volumes:
            return "unreadable"
        worst = volumes[0]
        warned = bool((self.central.get_state(DISK_LOW_KEY) or "").strip())
        if worst.free_gb >= floor:
            # Re-armed on RECOVERY, never on a clock: a standing shortage says nothing further
            if warned:
                self.central.set_state(DISK_LOW_KEY, "")
            return "ok"
        if warned:
            return "warned"
        self.central.set_state(DISK_LOW_KEY, f"{worst.free_bytes}")
        log.warning("%.1f GB free on %s, under the %.1f GB floor", worst.free_gb,
                    worst.path, floor)
        self.central.add_inbox(
            project="os", level="warning",
            title=f"{worst.free_gb:.1f} GB free on {worst.path}",
            body=(f"{worst.free_gb:.1f} GB of {worst.total_gb:.0f} GB left, under the "
                  f"{floor:.1f} GB floor (`os.disk_free_warn_gb`). When it reaches zero "
                  f"every SQLite write fails and each invariant reports itself as "
                  f"`database or disk is full`.\n\n"
                  f"The OS reclaims the checkout of a settled work order itself "
                  f"({self.catalog.os.worktree.reclaim_after_hours}h after it settles); "
                  f"what is left is the checkouts of open and failed orders, the logs "
                  f"under {logs_dir()} and Claude Code's transcripts."),
        )
        return "low"

    def check_ui_log(self) -> int:
        """Raise an inbox item for dashboard errors nobody has been told about yet"""
        from . import uilog

        errors, cursor = uilog.read_errors(self.central.get_state("ui_log_cursor") or "")
        self.central.set_state("ui_log_cursor", cursor)
        if not errors:
            return 0
        # One item per batch, not per error: a crash loop must not flood the inbox (and Telegram) with...
        latest = errors[-1]
        paths = sorted({e.path for e in errors})
        body = (f"Latest: {latest.summary}\n"
                f"Affected: {', '.join(paths[:5])}"
                f"{f' (+{len(paths) - 5} more)' if len(paths) > 5 else ''}\n"
                f"Traceback: {uilog.ui_log_path()}")
        self.central.add_inbox(
            project="os", level="warning",
            title=f"dashboard raised {len(errors)} unhandled error"
                  f"{'s' if len(errors) != 1 else ''}",
            body=body,
        )
        log.warning("dashboard errors: %d new (latest: %s)", len(errors), latest.summary)
        return len(errors)

    def check_ui_health(self, now: float | None = None) -> str:
        """Is the dashboard still serving, and restart it if it is not (docs/superpowers/specs/2026-10-08-the-dashboard-reports-and-heals-its-own-wedge.md)"""
        from . import db, release, service_manager, uilog
        from .notify import ui_base_url

        cfg = self.catalog.os.ui_health
        if not cfg.enabled:
            return "off"
        now = db.now() if now is None else now
        try:
            body = fetch_healthz(f"{ui_base_url(self.catalog)}/healthz",
                                 cfg.healthz_timeout_seconds)
        except Exception:  # noqa: BLE001 — an unreachable dashboard is the finding
            body = None
        wedged = body is None or bool(body.get("wedged"))
        if not wedged:
            self.central.set_state(UI_HEALTHZ_OK_AT, f"{now:.3f}")
            self.central.set_state(UI_WEDGE_CAPPED_AT, "")
            return "healthy"
        # Deliberately stopped, not wedged
        if not service_manager.unit_active(release.UI_UNIT):
            return "down"
        # Still booting after a release restart, not wedged (issue #1189)
        if body is None:
            # The STOP phase of that restart: uptime still holds the OLD start (issue #1194)
            if service_manager.unit_transitioning(release.UI_UNIT):
                log.info("dashboard not answering while %s is mid-restart — "
                         "not wedged", release.UI_UNIT)
                return "restarting"
            age = service_manager.uptime_seconds(release.UI_UNIT)
            if age is not None and age < cfg.boot_grace_seconds:
                log.info("dashboard not answering %.1fs after %s started — booting, "
                         "not wedged", age, release.UI_UNIT)
                return "booting"
        if not (self.central.get_state(UI_HEALTHZ_OK_AT) or "").strip():
            return "unseen"

        figures = (body or {}).get("limiter") or {}
        dump = (body or {}).get("stack_dump") or _existing(uilog.stack_dump_path())
        evidence = _wedge_evidence(figures, dump)
        restarts = [t for t in self._ui_restarts() if now - t < 24 * 3600]
        if restarts and now - max(restarts) < cfg.restart_cooldown_seconds:
            # Stops a restart loop against a dashboard that wedges on boot
            return "cooldown"
        if len(restarts) >= cfg.max_restarts_per_day:
            # At the cap the check keeps probing and the UI keeps dumping
            if not (self.central.get_state(UI_WEDGE_CAPPED_AT) or "").strip():
                self.central.set_state(UI_WEDGE_CAPPED_AT, f"{now:.3f}")
                self.central.add_inbox(
                    project="os", level="critical",
                    title="the dashboard is wedged and its restart cap is spent",
                    body=(f"`{release.UI_UNIT}` has been restarted "
                          f"{len(restarts)} times in the last 24h, which is the cap "
                          f"(`ui_health.max_restarts_per_day` = "
                          f"{cfg.max_restarts_per_day}). It will NOT be restarted again "
                          f"until that window rolls — a fourth wedge in a day is a "
                          f"different, worse fault than the one a restart fixes.\n"
                          f"{evidence}"),
                )
            return "capped"

        runner = self.release_runner or release.service_runner()
        # ATTEMPTED BEFORE IT IS ANNOUNCED, and the attempt is recorded whether or not it raised
        error = ""
        try:
            runner.restart_unit(release.UI_UNIT)
        except Exception as e:  # noqa: BLE001 — report what happened, do not re-raise
            error = f"{type(e).__name__}: {e}"
        restarts.append(now)
        self.central.set_state(UI_WEDGE_RESTARTS,
                               db.to_json([round(t, 3) for t in restarts]))
        self.central.set_state(UI_WEDGE_CAPPED_AT, "")
        if error:
            self.central.add_inbox(
                project="os", level="critical",
                title=f"the dashboard is wedged and {release.UI_UNIT} could not be "
                      f"restarted",
                body=(f"The restart was attempted and FAILED: {error}. The dashboard is "
                      f"still wedged and the OS cannot heal it — restart it by hand "
                      f"(`{service_manager.restart_hint(release.UI_UNIT)}`).\n"
                      f"The attempt counts against `ui_health.max_restarts_per_day`, so "
                      f"this is said once rather than on every tick.\n"
                      f"{evidence}"),
            )
            log.warning("dashboard wedged: restarting %s failed (%s)",
                        release.UI_UNIT, error)
            return "restart_failed"
        self.central.add_inbox(
            project="os", level="warning",
            title=f"the dashboard was wedged — restarted {release.UI_UNIT}",
            body=(f"Every routed page had stopped answering while the process stayed "
                  f"`active (running)`.\n"
                  + ("The dashboard dumped every thread's Python stack before the "
                     "restart; that dump is the only evidence of what held the "
                     "threadpool.\n" if dump else
                     "The dashboard wrote NO stack dump, so nothing says what held "
                     "it — it never answered at all.\n")
                  + evidence),
        )
        log.warning("dashboard wedged: restarted %s (%d in 24h)",
                    release.UI_UNIT, len(restarts))
        return "restarted"

    def _ui_restarts(self) -> list[float]:
        """When this daemon has self-restarted the dashboard, as epochs"""
        rows = db.from_json(self.central.get_state(UI_WEDGE_RESTARTS), []) or []
        out: list[float] = []
        for row in rows if isinstance(rows, list) else []:
            try:
                out.append(float(row))
            except (TypeError, ValueError):
                continue
        return out

    # -- 1. notifications

    def route_outbox(self, project: ProjectSpec, store: ProjectStore) -> None:
        for n in store.unrouted_notifications():
            self.central.add_inbox(
                project=project.name,
                title=n["title"],
                body=n["body"],
                level=n["level"],
                wo_id=n["wo_id"],
                paired_reason=n.get("paired_reason"),
            )
            store.mark_notification_routed(n["id"])

    # -- 2b. self-healing after the transport fails

    def retry_paused_turns(self, project: ProjectSpec, store: ProjectStore,
                           state: fleet.Fleet | None = None) -> None:
        """Relaunch the turns the transport lost, once their wait is up (#259)"""
        budget = project.max_concurrent - store.count_active()
        for wo in store.list_work_orders(statuses=RETRY_SWEEP_STATUSES):
            held = state.blocked(wo["id"]) if state is not None else ""
            try:
                pause = retries_turn(store, wo)
            except Exception:  # noqa: BLE001 — one work order must not stall the rest
                log.exception("[%s] could not diagnose %s", project.name, wo["id"])
                continue
            if pause is None:
                continue
            # BELOW the filter above, so only an order that was going to be relaunched on this pass is ever...
            if held:
                assert state is not None  # `held` is only ever set from a reading
                cause = state.hold_cause(wo["id"]) or "fleet_cap"
                figures: dict[str, Any] = {"in_flight": state.in_flight, "cap": state.cap}
                if cause == "fleet_outage":
                    # Not relabelled a cap: `invariants.fleet_hold_note` owns the words for an outage (issue #714)
                    figures["reopens_at"] = state.outage.reopens_at
                elif cause == "fleet_ramp":
                    figures.update(cap=state.ramp.cap, until=state.ramp.until,
                                   since=state.ramp.since)
                elif cause == "fleet_paused":
                    figures["since"] = state.pause.since
                self._record_retry_held(store, wo, pause, cause=cause, figures=figures)
                continue
            takes_a_slot = resume_spends_slot(wo)
            if takes_a_slot and budget <= 0:
                self._record_retry_held(
                    store, wo, pause, cause="project_cap",
                    figures={"active": store.count_active(),
                             "max_concurrent": project.max_concurrent})
                continue
            try:
                # COLD, AND LARGE: compact first (issue #843)
                turn = worker_session.compact_before_relaunch(
                    store, project, wo, self.catalog.os.compact_min_context,
                    pause=pause) or worker_session.retry(store, project, wo, pause)
            except budget_mod.BudgetExhausted as e:
                # `NOT_RETRIED` already keeps this sweep off an order already parked in `budget_exhausted`
                budget_mod.escalate(store, wo, e.exhausted)
                continue
            except claude_cli.ClaudeCliError as e:
                # Not fatal and not the user's problem yet: the next pass tries again
                log.warning("[%s] retry of %s (%s) failed: %s",
                            project.name, wo["id"], pause.reason, e)
                continue
            except Exception:  # noqa: BLE001 — one work order must not stall the rest
                log.exception("[%s] retry of %s failed", project.name, wo["id"])
                continue
            if turn is None:
                # The paused compaction had landed: what is queued behind it goes out on delivery
                log.info("[%s] %s: compaction turn %s had landed, nothing relaunched",
                         project.name, wo["id"], pause.turn["seq"])
                continue
            if state is not None:
                state.launched()
            if takes_a_slot:
                budget -= 1
            log.info("[%s] %s resumed after %s (attempt %s/%s, turn %s)",
                     project.name, wo["id"], pause.reason, pause.attempts,
                     pause.max_attempts or "∞", turn["seq"])
            store.add_event(wo["id"], "turn_resumed", {
                "seq": turn["seq"], "retried_seq": pause.turn["seq"],
                "attempt": pause.attempts, "of": pause.max_attempts,
                "reason": pause.reason,
                # WHERE THIS TURN CAME FROM, read back by `ops.resumed_from` when it settles
                "was": wo["status"],
            })
            # The turn is out, so the record must stop saying somebody else has it (#259)
            if wo["status"] != "running":
                store.set_status(wo["id"], "running")
                # ONLY THE PAUSE'S OWN FLAG
                if wo["attention_reason"] == invariants_mod.AUTH_BLOCKER:
                    store.clear_attention(wo["id"])

    def resume_budget_held(self, project: ProjectSpec, store: ProjectStore,
                           state: fleet.Fleet | None = None) -> None:
        """Relaunch the raised-budget orders the user's pause held, once it lets them through (#1130)"""
        from . import ops as ops_mod

        for wo in store.list_work_orders(statuses=(budget_mod.EXHAUSTED,)):
            if not ops_mod.budget_resume_outstanding(store, wo["id"]):
                continue
            pause = state.pause if state is not None else fleet.load_pause(self.central)
            if pause is not None and not pause.allows(wo["id"]):
                continue
            try:
                resumed, note = ops_mod.resume_raised_budget(
                    store, self.central, project.name, wo, None)
            except Exception:  # noqa: BLE001 — one work order must not stall the rest
                log.exception("[%s] budget resume of %s failed", project.name, wo["id"])
                continue
            store.add_event(wo["id"], ops_mod.BUDGET_RESUME_RELEASED,
                            {"resumed": resumed, "note": note})
            log.info("[%s] %s: held budget resume released — %s",
                     project.name, wo["id"], note)

    @staticmethod
    def _record_retry_held(store: ProjectStore, wo: dict, pause: Any, *,
                           cause: str, figures: dict[str, Any]) -> None:
        """Write down that this due pause was deferred — at most once every restate (docs/superpowers/specs/2026-09-25-a-cap-hold-must-say-so.md)"""
        last = store.last_event_of_kind(wo["id"], invariants_mod.RETRY_HELD_EVENT)
        if last is not None:
            payload = db.from_json(last.get("payload"), {}) or {}
            if (payload.get("seq") == pause.turn["seq"]
                    and db.now() - float(last.get("ts") or 0) < RETRY_HELD_RESTATE):
                return
        store.add_event(wo["id"], invariants_mod.RETRY_HELD_EVENT,
                        {"cause": cause, "seq": pause.turn["seq"],
                         "reason": pause.reason, "retry_at": pause.retry_at, **figures})

    # -- 3. message delivery

    def deliver_messages(self, project: ProjectSpec, store: ProjectStore,
                         state: fleet.Fleet | None = None) -> None:
        """Send queued user messages into their work orders' conversations (#134)"""
        pending: dict[str, list[dict[str, Any]]] = {}
        # `deliverable_`, not `queued_`: a message whose last delivery attempt failed is held by its backoff...
        for msg in store.deliverable_messages():  # chronological, so joins stay in order
            try:
                wo = store.get_work_order(msg["wo_id"])
            except KeyError:
                store.mark_message(msg["id"], "failed")
                continue
            # BEFORE the holds, deliberately: a STOPPED order's hold would skip this message for ever instead of...
            status = str(wo["status"] or "")
            if status in TERMINAL_STATUSES or status == "needs_review":
                envelope = store.envelope_for_message(int(msg["id"]))
                if envelope and envelope["kind"] == "assumption_objection":
                    self._withdraw_objection(
                        store, wo["id"], envelope=envelope,
                        reason=(OBJECTION_HANDED_BACK_REASON if status == "needs_review"
                                else OBJECTION_WITHDRAWN_REASON))
                    continue
            # Every hold, in the one place that decides them (`worker_session.message_hold`
            if worker_session.message_hold(store, wo, msg) is not None:
                continue
            pending.setdefault(wo["id"], []).append(dict(msg))
        # Chronological, because `queued_messages` is and a dict keeps insertion order: the oldest waiting...
        budget = project.max_concurrent - store.count_active()
        for wo_id, msgs in pending.items():
            wo = store.get_work_order(wo_id)
            # UNDER THE FLEET TOO (issue #843)
            held = state.blocked(wo_id) if state is not None else ""
            if held:
                log.debug("[%s] holding %s message(s) for %s: %s",
                          project.name, len(msgs), wo_id, held)
                continue
            if resume_spends_slot(wo):
                if budget <= 0:
                    log.debug("[%s] holding %s message(s) for %s: all %s slots are full",
                              project.name, len(msgs), wo_id, project.max_concurrent)
                    continue
                budget -= 1
            if self._deliver(project, store, wo, msgs) and state is not None:
                state.launched()

    def deliver_envelopes(self, project: ProjectSpec, store: ProjectStore) -> None:
        """Route every queued envelope, oldest first (src/jarvis/bus.py)"""
        for envelope in store.queued_envelopes():
            try:
                state = bus.deliver(store, self.central, envelope,
                                    project=project.name)
            except Exception:  # noqa: BLE001
                log.exception("[%s] envelope %s could not be routed",
                              project.name, envelope["id"])
                continue
            if state != "delivered":
                log.info("[%s] envelope %s -> %s", project.name, envelope["id"], state)

    # -- 3b. the validation round machine (see the validation-panel design)

    def validation_tick(self, project: ProjectSpec, store: ProjectStore,
                        state: fleet.Fleet | None = None) -> None:
        """Judge every work order with an open round, off this thread (docs/superpowers/specs/2026-09-13-two-gates-not-a-chain.md §3)"""
        if state is not None and state.shut():
            # Nothing would be judged; every seat would be refused free and instantly (#714)
            self._hold_rounds_for_outage(project, store, state)
            return
        for wo in store.work_orders_awaiting_validation():
            wo_id = wo["id"]
            if wo_id in self.validating:
                continue  # its round is in flight; a second tick must not start another
            if state is not None and state.pause is not None \
                    and not state.pause.allows(wo_id):
                continue  # the user paused the fleet; the panel spends on this too (#843)
            round_row = runnable_round(store, wo)
            if round_row is None:
                continue
            self.validating.add(wo_id)
            future = self.validate_pool.submit(
                self._validate_work_order, project, wo_id, int(round_row["id"]))
            future.add_done_callback(
                lambda f, k=wo_id: self.validating.discard(k))
        # A `validating` row with NO round is the one shape the query above cannot select on
        for orphan in store.list_work_orders(statuses=("validating",),
                                             include_hidden=True):
            if store.latest_validation_round(wo_id=orphan["id"]) is None:
                log.warning("[%s] %s is validating with no round on record",
                            project.name, orphan["id"])

    def _hold_rounds_for_outage(self, project: ProjectSpec, store: ProjectStore,
                                state: fleet.Fleet) -> None:
        """Write down that the account's window — not this round — is why nobody judged"""
        reopens = state.outage.reopens_at  # type: ignore[union-attr]
        for wo in store.work_orders_awaiting_validation():
            wo_id = wo["id"]
            if wo_id in self.validating:
                continue
            round_row = store.latest_validation_round(wo_id=wo_id)
            if round_row is None:  # pragma: no cover - the query selected on this round
                continue
            n = int(round_row["round"])
            if validation_hold_until(store.events_of_kind(wo_id, "validation_failed"),
                                     n) >= reopens:
                continue
            store.add_event(wo_id, "validation_failed",
                            {"round": n, "cause": VALIDATION_HELD_CAUSE,
                             "reopens_at": reopens,
                             "error": state.outage.message[:500]})  # type: ignore[union-attr]
            log.info("[%s] %s round %s held by the account's usage window until %s",
                     project.name, wo_id, n, reopens)

    def _hold_feature_rounds_for_outage(self, project: ProjectSpec, store: ProjectStore,
                                        state: fleet.Fleet) -> None:
        """`_hold_rounds_for_outage` for feature rounds — read that one; this is its twin"""
        from . import ops

        reopens = state.outage.reopens_at  # type: ignore[union-attr]
        for fo in store.list_feature_orders(statuses=("validating",)):
            fo_id = fo["id"]
            if fo_id in self.validating:
                continue
            round_row = store.latest_validation_round(fo_id=fo_id)
            if (round_row is None
                    or round_row["outcome"] not in RUNNABLE_VALIDATION_OUTCOMES):
                continue
            n = int(round_row["round"])
            events = ops.feature_events_of_kind(store, fo_id, "validation_failed")
            if validation_hold_until(events, n) >= reopens:
                continue
            ops.feature_event(store, fo_id, "validation_failed",
                              {"round": n, "cause": VALIDATION_HELD_CAUSE,
                               "reopens_at": reopens, "feature_order": fo_id,
                               "error": state.outage.message[:500]})  # type: ignore[union-attr]
            log.info("[%s] feature %s round %s held by the account's usage window "
                     "until %s", project.name, fo_id, n, reopens)

    def _validate_work_order(self, project: ProjectSpec, wo_id: str,
                             round_id: int) -> None:
        """One round, start to finish (runs on the single validate thread)"""
        from . import evidence as evidence_mod
        from . import ops, specs

        store = ProjectStore(project.path)  # thread-local connection — see the docstring
        try:
            wo = store.get_work_order(wo_id)
            round_row = store.get_validation_round(round_id)
            if round_row is None:  # pragma: no cover - deleted mid-flight
                return
            cfg = self._round_config(project, round_row)
            n, max_rounds = int(round_row["round"]), int(cfg.max_rounds)
            # WHICH BUDGET THIS ROUND CAME OUT OF
            uncounted = bool(round_row["uncounted"])
            # WHICH EXEMPTION, not merely that there was one
            cause = str(round_row["uncounted_cause"] or "")
            as_rebind = uncounted and cause != ops.USER_REWORK_CAUSE
            rebinds = store.uncounted_validation_rounds(
                wo_id=wo_id, cause=cause or None) if as_rebind else 0
            pos = store.counted_validation_rounds(wo_id=wo_id) + (
                0 if str(round_row["outcome"] or "") in COUNTED_VALIDATION_OUTCOMES
                else 1)
            goes_back = (rebinds < ops.REBIND_MAX) if as_rebind else (pos < max_rounds)
            # THE §7.2 PRE-CHECK RUNS HERE AND ACTS LOWER DOWN
            unproven: list[str] = []
            central = CentralStore()          # thread-local — see the docstring
            try:
                refusal = ops.fix_order_precheck(store, project.path, wo,
                                                 central=central, unproven=unproven)
            finally:
                central.close()
            packet = evidence_mod.collect_work_order(
                project.path, wo, declared=str(round_row["evidence"] or ""),
                # WHAT THE OS COULD NOT DECIDE ITSELF
                unproven=tuple(unproven),
                diff_chars=cfg.diff_chars, spec=specs.spec_of(store, wo),
                side_effects=ops.side_effects_of(store, wo_id),
                assumptions=store.all_assumptions(wo_id),
                # WHAT EARLIER ROUNDS ALREADY ASKED FOR
                history=ops.prior_round_history(store, wo_id=wo_id, before=n))
            # WHICH COMMIT THIS ROUND IS JUDGING
            store.set_validation_head(round_id, evidence_mod.judged_head(packet))
            # Beside the head and for its reason: what the NEXT submission has to diff itself against to learn... (docs/superpowers/specs/2026-09-22-a-round-must-answer-the-list.md §3)
            store.set_validation_file_shas(round_id, packet.file_shas)

            # WAIT FOR CI RATHER THAN FOR THE WORKER TO PROVE THE SUITE ITSELF
            pending = evidence_mod.ci_pending(packet)
            if pending and time.time() < float(round_row["ts"]) + CI_HOLD_DEADLINE_SECONDS:
                self._validation_ci_held(store, wo, round_id, n, pending)
                log.info("[%s] %s: round %d held for CI (%s)",
                         project.name, wo_id, n, ", ".join(pending))
                return

            # A HEAD GITHUB HAS ALREADY CALLED RED IS SENT BACK, not judged (docs/superpowers/specs/2026-10-13-a-red-head-is-sent-back-before-the-panel.md)
            failing = evidence_mod.ci_failing(packet)
            returns = store.uncounted_validation_rounds(
                wo_id=wo_id, cause=ops.RED_CI_CAUSE)
            if not pending and failing and returns < int(cfg.red_ci_returns):
                self._validation_red_ci(store, wo, round_id, n, max_rounds, failing)
                log.info("[%s] %s: round %d returned unjudged — CI is red (%s)",
                         project.name, wo_id, n, ", ".join(failing))
                return

            validator = (self.validator if self.validator is not None
                         else self._validator(cfg))
            if validator is None:
                # Nothing to judge with — the panel is not wired in
                store.settle_validation_round(
                    round_id, "failed",
                    {"round": n, "cause": "no_validator",
                     "reason": NO_VALIDATOR_REASON},
                    wo_id=wo_id, reason=NO_VALIDATOR_REASON)
                # `panel_cleared`: the round this just closed is `failed`
                ops.land_when_cleared(store, wo, panel_cleared=True)
                log.info("[%s] %s: round %d settled unjudged (no validator)",
                         project.name, wo_id, n)
                return

            # A SUBMISSION WITH NOTHING FOR A REVIEWER never reaches the validator (docs/superpowers/specs/2026-09-17-a-round-with-nothing-to-judge.md §3)
            empty = evidence_mod.nothing_to_judge(packet)
            if empty == "escalate":
                self._escalate(store, wo, round_id, n,
                               "this submission changes no files and records no other "
                               "durable effect, so there is nothing to review. Nobody "
                               "has judged the work.",
                               cause=ops.ESCALATION_NOTHING_TO_JUDGE)
                return
            if empty == "void":
                self._void(store, wo, round_id, n, evidence_mod.void_reason(packet))
                log.info("[%s] %s: round %d voided — nothing for a reviewer to judge",
                         project.name, wo_id, n)
                return

            # THE §7.2 PRE-CHECK ACTS, A SECOND TIME (docs/superpowers/specs/2026-09-27-self-evolution.md §7.2.)
            if refusal is not None:
                self._reject(store, wo, round_id, n, max_rounds,
                             refusal + ops.PRECHECK_SPENT_NOTE, budget_round=pos,
                             rebind=(rebinds, ops.REBIND_MAX) if as_rebind else None,
                             uncounted_cause=cause)
                log.info("[%s] %s: round %d refused before the panel — %s",
                         project.name, wo_id, n, refusal)
                return

            # A REPEAT of the IMMEDIATELY PRECEDING round only
            previous = self._preceding_round(store, n, wo_id=wo_id)
            # A SUBMISSION SENT BACK FOR RED CI MAY COME BACK UNCHANGED (#1023)
            after_red_ci = (previous is not None and not failing
                            and str(previous["uncounted_cause"] or "")
                            == ops.RED_CI_CAUSE)
            if self._repeat_submission(round_row, previous,
                                       after_red_ci=after_red_ci):
                self._escalate(
                    store, wo, round_id, n,
                    f"this submission is identical to round {previous['round']} — the "
                    f"same changes and the same declared evidence — so the review has "
                    f"nothing new to judge.",
                    cause=ops.ESCALATION_REPEAT_SUBMISSION)
                return

            failure: claude_cli.ClaudeCliError | None = None
            verdict = {}
            try:
                verdict = validator(store, dict(round_row), packet)
            except claude_cli.ClaudeCliError as e:
                failure = e

            # THE UNIT MAY HAVE SETTLED WHILE THE SEATS READ
            if not store.round_machine_owns(store.get_validation_round(round_id)):
                log.info("[%s] %s: round %d settled underneath the panel — dropping "
                         "the verdict", project.name, wo_id, n)
                return

            if isinstance(failure, claude_cli.UsageLimitError):
                # BEFORE the generic outage below
                self._validation_held(store, wo, round_id, n, failure.limit)
                log.info("[%s] %s: round %d held until the usage window reopens",
                         project.name, wo_id, n)
                return
            if isinstance(failure, claude_cli.AuthFailureError):
                # BEFORE the generic outage below
                self._validation_auth_held(store, wo, round_id, n, failure.auth)
                log.info("[%s] %s: round %d held until Claude Code can authenticate",
                         project.name, wo_id, n)
                return
            if isinstance(failure, claude_cli.PromptTooLargeError):
                # BEFORE the generic outage below (docs/superpowers/specs/2026-09-26-bounded-model-inputs.md)
                self._validation_refused(store, project, wo, round_id, n, failure)
                return
            if isinstance(failure, claude_cli.KilledError):
                # BEFORE the generic outage below (docs/superpowers/specs/2026-10-14-a-killed-validation-panel-opens-an-investigation.md)
                self._validation_killed(store, project, wo, round_id, n,
                                        failure.killed, cfg)
                return
            if failure is not None:
                self._validation_outage(store, wo, round_id, n, failure)
                return

            for seat in verdict.get("seats") or ():
                store.record_validation_opinion(
                    round_id, str(seat.get("seat") or ""),
                    reply=str(seat.get("reply") or ""),
                    verdict=str(seat.get("verdict") or ""),
                    status=str(seat.get("status") or "ok"),
                    model=str(seat.get("model") or ""),
                    latency_ms=int(seat.get("latency_ms") or 0))
            # IN THE OUTCOME BRANCH, not before it: a follow-up is filed when the LOOP SETTLES
            follow_ups = verdict.get("follow_ups") or ()
            outcome = str(verdict.get("outcome") or "")
            reason = str(verdict.get("reason") or "")

            if outcome == "passed":
                store.settle_validation_round(
                    round_id, "passed", {"round": n, "round_id": round_id},
                    wo_id=wo_id, reason=reason)
                self._file_follow_ups(store, project, round_row, follow_ups, cfg,
                                      unit=wo_id, wo_id=wo_id)
                # NOT UNDER A LIVE TURN (docs/superpowers/specs/2026-09-25-a-cap-hold-must-say-so.md)
                if worker_session.busy(store, wo_id) is None:
                    status = ops.land_when_cleared(store, wo)
                else:
                    status = "deferred — a worker turn is in flight"
                    store.add_event(wo_id, VALIDATION_LANDING_DEFERRED,
                                    {"round": n, "round_id": round_id,
                                     "outcome": "passed"})
                log.info("[%s] %s passed review in round %d -> %s",
                         project.name, wo_id, n, status)
            elif outcome == "rejected" and goes_back:
                # The inadmissible follow-ups ride along HERE and nowhere else: a stale docstring or a missing test...
                self._reject(store, wo, round_id, n, max_rounds, reason,
                             note=ops.follow_up_feedback(follow_ups), budget_round=pos,
                             rebind=(rebinds, ops.REBIND_MAX) if as_rebind else None,
                             uncounted_cause=cause)
                log.info("[%s] %s rejected in round %d of %d",
                         project.name, wo_id, n if uncounted else pos, max_rounds)
            elif outcome == "rejected":
                # The last round it had, and it was refused
                self._file_follow_ups(store, project, round_row, follow_ups, cfg,
                                      unit=wo_id, wo_id=wo_id)
                # WHICH of the three handovers this is
                spent = (ops.ESCALATION_REBINDS_SPENT if as_rebind
                         else ops.ESCALATION_USER_REWORK_CEILING
                         if uncounted and cause == ops.USER_REWORK_CAUSE
                         else ops.ESCALATION_ROUNDS_SPENT)
                self._escalate(store, wo, round_id, n, reason or (
                    f"the review was not satisfied after {max_rounds} rounds."),
                    cause=spent)
            else:
                # `escalated`, or anything the validator returned that is not a verdict this machine knows
                self._escalate(store, wo, round_id, n, reason or (
                    "the review could not reach a verdict."),
                    cause=ops.ESCALATION_NO_VERDICT)
        except Exception:  # noqa: BLE001 — a round must never kill the daemon
            log.exception("[%s] validating %s failed", project.name, wo_id)
        finally:
            store.close()

    @staticmethod
    def _file_follow_ups(store: ProjectStore, project: ProjectSpec,
                         round_row: Any, follow_ups: Any, cfg: Any, *, unit: str,
                         wo_id: str | None = None, fo_id: str | None = None) -> None:
        """File this round's non-blocking findings, and say so in the log"""
        from . import ops

        try:
            filed = ops.file_validation_follow_ups(
                store, project, round_row, follow_ups, cfg, wo_id=wo_id, fo_id=fo_id)
        except Exception:  # noqa: BLE001 — see the docstring
            log.exception("[%s] %s: filing follow-ups failed", project.name, unit)
            return
        if filed["items"] or filed["dropped"] or filed["failed"]:
            log.info("[%s] %s: round %d filed %d follow-up issue(s), dropped %d over the "
                     "cap, %d could not be filed%s",
                     project.name, unit, filed["round"], len(filed["items"]),
                     filed["dropped"], filed["failed"],
                     f" ({filed['reason']})" if filed.get("reason") else "")

    @staticmethod
    def _repeat_submission(round_row: dict[str, Any],
                           previous: dict[str, Any] | None, *,
                           after_red_ci: bool = False) -> bool:
        """Is this round the SAME SUBMISSION as the one before it, with nothing new? (docs/superpowers/specs/2026-09-15-forcing-a-validation-round.md §7)"""
        if str(round_row["forced_reason"] or "").strip() or after_red_ci:
            return False
        return (previous is not None
                and previous["fingerprint"] == round_row["fingerprint"])

    @staticmethod
    def _preceding_round(store: ProjectStore, n: int, *, wo_id: str | None = None,
                         fo_id: str | None = None) -> dict[str, Any] | None:
        """The round before this one, by number"""
        for row in store.validation_rounds(wo_id=wo_id, fo_id=fo_id):
            if int(row["round"]) == n - 1:
                return row
        return None

    @staticmethod
    def _reject(store: ProjectStore, wo: dict, round_id: int, n: int, max_rounds: int,
                reason: str, *, note: str = "", budget_round: int | None = None,
                rebind: tuple[int, int] | None = None,
                uncounted_cause: str = "", failing: tuple[str, ...] = (),
                pr_url: str = "") -> None:
        """Close the round and send the feedback back — OVER THE BUS, never directly (docs/superpowers/specs/2026-10-13-a-red-head-is-sent-back-before-the-panel.md)"""
        from . import ops

        wo_id = wo["id"]
        rework = uncounted_cause == ops.USER_REWORK_CAUSE
        red_ci = uncounted_cause == ops.RED_CI_CAUSE
        note += ops.non_landing_review_note(store, wo)
        used, of = rebind or (0, max_rounds)
        pos = n if budget_round is None else budget_round
        # `of` is the bound this round was actually held by
        event: dict[str, Any] = {"round": n, "round_id": round_id, "of": of,
                                 "budget_round": pos}
        if rebind is not None or rework or red_ci:
            event["uncounted"] = True
        if uncounted_cause:
            event["uncounted_cause"] = uncounted_cause
        if failing:
            # WHAT IT RETURNED FOR, truncated like the envelope's: GitHub's check list has moved on by the time...
            event["failing"] = list(failing[:6])
        store.settle_validation_round(round_id, "rejected", event, wo_id=wo_id,
                                      reason=reason)
        if red_ci:
            text = RED_CI_FEEDBACK.format(reason=reason + note, pr_url=pr_url,
                                          wo_id=wo_id, max_rounds=max_rounds)
        elif rework:
            text = USER_REWORK_FEEDBACK.format(max_rounds=max_rounds,
                                               reason=reason + note, wo_id=wo_id)
        elif rebind is not None:
            text = REBIND_FEEDBACK.format(used=used, max=of, max_rounds=max_rounds,
                                          reason=reason + note, wo_id=wo_id)
        else:
            text = REVIEW_FEEDBACK.format(n=pos, max=max_rounds, reason=reason + note,
                                          wo_id=wo_id)
        bus.post(store, subject=bus.Subject(wo_id=wo_id),
                 from_role="reviewer", to_role="implementor",
                 payload=bus.ReviewFeedback(round=n, outcome="rejected", reason=text))

    @staticmethod
    def _void(store: ProjectStore, wo: dict, round_id: int, n: int,
              reason: str) -> None:
        """Close a round that had nothing for a reviewer, WITHOUT asking the user (docs/superpowers/specs/2026-09-17-a-round-with-nothing-to-judge.md §7.)"""
        from . import ops

        wo_id = wo["id"]
        store.settle_validation_round(
            round_id, "void", {"round": n, "round_id": round_id, "reason": reason},
            wo_id=wo_id, reason=reason)
        ops.land_when_cleared(store, wo, panel_cleared=True)

    @staticmethod
    def _escalate(store: ProjectStore, wo: dict, round_id: int, n: int,
                  reason: str, *, cause: str) -> None:
        """Give up on this unit and ask the user (docs/superpowers/specs/2026-10-14-an-escalated-round-says-why-and-what-to-run-next.md §2.)"""
        from . import ops

        ops.escalate_validation_round(store, wo, round_id, n, reason, cause=cause)

    @staticmethod
    def _validation_held(store: ProjectStore, wo: dict, round_id: int, n: int,
                         limit: claude_cli.UsageLimit) -> None:
        """The account's window is spent (#235)"""
        from .invariants import usage_hold_note

        reopens = limit.reset_at or (db.now()
                                     + worker_session.RATE_LIMIT_FALLBACK_DELAY)
        store.settle_validation_round(
            round_id, "failed",
            {"round": n, "cause": VALIDATION_HELD_CAUSE,
             "reopens_at": reopens, "error": limit.message[:500]},
            wo_id=wo["id"], reason=usage_hold_note(reopens),
            hold_cause=VALIDATION_HELD_CAUSE)

    @staticmethod
    def _validation_red_ci(store: ProjectStore, wo: dict, round_id: int, n: int,
                           max_rounds: int, failing: tuple[str, ...]) -> None:
        """GitHub has finished and says the code is wrong (docs/superpowers/specs/2026-10-13-a-red-head-is-sent-back-before-the-panel.md)"""
        from . import ops

        names = ", ".join(failing[:6]) + (" …" if len(failing) > 6 else "")
        store.mark_validation_round_uncounted(round_id, ops.RED_CI_CAUSE)
        # SELF-DESCRIBING, because a seat reads this as history next round and must not take it for a quality...
        Daemon._reject(
            store, wo, round_id, n, max_rounds,
            f"the checks on this pull request have finished and these are red: {names}. "
            f"It was sent back without review rather than judged.",
            uncounted_cause=ops.RED_CI_CAUSE, failing=failing,
            pr_url=str(wo["pr_url"] or ""))

    @staticmethod
    def _validation_ci_held(store: ProjectStore, wo: dict, round_id: int, n: int,
                            pending: tuple[str, ...]) -> None:
        """GitHub has not finished running the checks"""
        reopens = time.time() + CI_HOLD_RECHECK_SECONDS
        names = ", ".join(pending[:6]) + (" …" if len(pending) > 6 else "")
        store.settle_validation_round(
            round_id, "failed",
            {"round": n, "cause": VALIDATION_CI_CAUSE,
             "reopens_at": reopens, "pending": list(pending)},
            wo_id=wo["id"],
            reason=f"waiting for GitHub to finish the checks on this pull request "
                   f"before judging it: {names}",
            hold_cause=VALIDATION_CI_CAUSE)

    @staticmethod
    def _validation_auth_held(store: ProjectStore, wo: dict, round_id: int, n: int,
                              auth: claude_cli.AuthFailure) -> None:
        """Claude Code could not authenticate (#778)"""
        from .invariants import AUTH_HOLD_NOTE

        wo_id = wo["id"]
        attempt = 1 + sum(
            1 for e in store.events_of_kind(wo_id, "validation_failed")
            if db.from_json(e["payload"], {}).get("round") == n
            and db.from_json(e["payload"], {}).get("cause") == VALIDATION_AUTH_CAUSE)
        reopens = time.time() + AUTH_HOLD_BACKOFF[min(attempt - 1,
                                                      len(AUTH_HOLD_BACKOFF) - 1)]
        store.settle_validation_round(
            round_id, "failed",
            {"round": n, "cause": VALIDATION_AUTH_CAUSE,
             "reopens_at": reopens, "attempt": attempt,
             "error": auth.message[:500]},
            wo_id=wo_id, reason=AUTH_HOLD_NOTE, hold_cause=VALIDATION_AUTH_CAUSE)

    def _validation_refused(self, store: ProjectStore, project: ProjectSpec, wo: dict,
                            round_id: int, n: int,
                            error: claude_cli.PromptTooLargeError) -> None:
        """The OS refused its own seat prompt (docs/superpowers/specs/2026-09-26-bounded-model-inputs.md)"""
        from . import ops

        central = CentralStore()          # thread-local, as `_round_config` opens its own
        try:
            said = self._note_prompt_refusal(central, store, project.name, wo["id"],
                                             f"validation round {n}", error)
        finally:
            central.close()
        self._escalate(
            store, wo, round_id, n,
            f"the review could not be run: the OS built a {said['total']}-char prompt "
            f"for it, over the {said['ceiling']}-char ceiling ({said['setting']}), so "
            f"it was never sent. Nobody has judged the work.",
            cause=ops.ESCALATION_PROMPT_REFUSED)
        from . import invariants

        store.flag_attention(wo["id"], invariants.os_prompt_refused_blocker(said))

    def _validation_outage(self, store: ProjectStore, wo: dict, round_id: int, n: int,
                           error: Exception) -> None:
        """The validator could not be reached"""
        from . import ops

        wo_id = wo["id"]
        outages = 1 + sum(
            1 for e in store.events_of_kind(wo_id, "validation_failed")
            if db.from_json(e["payload"], {}).get("round") == n
            and db.from_json(e["payload"], {}).get("cause") == "transport")
        store.settle_validation_round(
            round_id, "failed",
            {"round": n, "cause": "transport", "attempt": outages,
             "error": str(error)[:500]},
            wo_id=wo_id, reason=f"the validator could not be reached: {error}")
        if outages >= VALIDATION_OUTAGE_LIMIT:
            self._escalate(
                store, wo, round_id, n,
                f"the review could not be run: the validator was unreachable "
                f"{outages} times in a row. Nobody has judged the work.",
                cause=ops.ESCALATION_VALIDATOR_UNREACHABLE)

    def _validation_killed(self, store: ProjectStore, project: ProjectSpec, wo: dict,
                           round_id: int, n: int, killed: claude_cli.Killed,
                           cfg: Any) -> None:
        """The seats' PROCESSES WERE KILLED (#1039)"""
        from . import ops

        wo_id = wo["id"]
        kills = 1 + sum(
            1 for e in store.events_of_kind(wo_id, "validation_failed")
            if db.from_json(e["payload"], {}).get("round") == n
            and db.from_json(e["payload"], {}).get("cause") == VALIDATION_KILLED_CAUSE)
        # A review that cannot run is an OS function broken: `critical`
        decision = steward.gate(
            steward.Candidate("investigation", "critical", project.name,
                              f"validation seats killed on {wo_id}",
                              evidence=(f"rc={killed.rc}",), parent_wo_id=wo_id),
            # NOT `self.central`: this runs on a validation thread
            catalog=self.catalog, store=store, self_dispatch=self.self_dispatch)
        inv_id = (self._killed_investigation(store, project, wo_id, round_id, n, killed)
                  if decision.allowed else "")
        store.settle_validation_round(
            round_id, "failed",
            {"round": n, "cause": VALIDATION_KILLED_CAUSE, "attempt": kills,
             "rc": killed.rc, "error": killed.message[:500],
             "investigation": inv_id},
            wo_id=wo_id,
            reason=f"every validation seat was killed by a signal (rc={killed.rc}) "
                   f"before it could judge this submission")
        if kills >= int(cfg.killed_retries):
            # THE INVESTIGATION ID RIDES ON THE SENTENCE: the user reading the attention item needs somewhere to...
            pointer = (f"{inv_id} is investigating what killed them." if inv_id
                       else "No investigation could be filed, so what killed them is "
                            "not being diagnosed by anything.")
            self._escalate(
                store, wo, round_id, n,
                f"the review could not be run: its seats were killed by a signal "
                f"(rc={killed.rc}) {kills} times in a row. Nobody has judged the work. "
                f"{pointer}",
                cause=ops.ESCALATION_PANEL_KILLED)

    @staticmethod
    def _killed_investigation(store: ProjectStore, project: ProjectSpec, wo_id: str,
                              round_id: int, n: int,
                              killed: claude_cli.Killed) -> str:
        """The live investigation on this order, filing one first if there is none"""
        from . import ops

        try:
            live = ops.live_investigation(project.name, wo_id)
            if live:
                return live
            seats_said = "\n".join(
                f"  - {row['seat']}: status={row['status']}, "
                f"latency_ms={int(row['latency_ms'] or 0)}, "
                f"rc={_seat_rc(str(row['reply'] or ''), killed.rc)}, "
                f"ended {_moment(float(row['ts'] or 0.0))}"
                for row in store.validation_opinions(round_id)) or "  - none recorded"
            round_row = store.get_validation_round(round_id) or {}
            opened = float(round_row.get("ts") or 0.0)
            order = ops.create_investigation_order(project.name, wo_id, why=(
                f"Validation round {n} of {wo_id} was never judged: its seats' "
                f"processes were killed by a signal (rc={killed.rc}) and wrote nothing. "
                f"Four concurrent seats ending at the same elapsed time with no bytes on "
                f"either stream is ONE EXTERNAL EVENT, not four model failures.\n\n"
                f"What the record holds (read it, do not take this as the evidence):\n"
                f"  - round id {round_id}, round {n}, opened {_moment(opened)}\n"
                f"  - the message the transport classified: {killed.message}\n"
                f"  - the seats, as the round recorded them:\n{seats_said}\n\n"
                f"What to check for what else happened at that instant:\n"
                f"  - this daemon's own restart and deploy record\n"
                f"  - `jarvis alarms` and `jarvis inspect {wo_id}` around those moments\n"
                f"  - the fleet pause state, and this order's budget holds\n"
                f"  - the `os.validation.timeout` in force when the round opened\n\n"
                f"WHAT NOT TO CONCLUDE: that a retry is the answer. The round IS "
                f"re-judged already (`validation.killed_retries` bounds it). A retry "
                f"cannot name what killed these processes and cannot stop it recurring; "
                f"naming the cause is this order's whole job. GitHub issue #1039."))
            return str(order["id"])
        except ops.OpsError as e:
            # NEVER PROPAGATES: a lost race between two ticks must not cost the round its own record (spec §4a)
            log.warning("[%s] %s: no investigation filed for the killed round: %s",
                        project.name, wo_id, e)
            return ""

    def _round_config(self, project: ProjectSpec, round_row: dict[str, Any]) -> Any:
        """The validation settings THE ROUND WAS OPENED UNDER, not the live ones"""
        from . import config_version as cv

        vid = round_row.get("config_version")
        if not vid:
            return project.validation
        central = CentralStore()  # thread-local connection — see the docstring
        try:
            row = central.get_config_version(str(vid))
        finally:
            central.close()
        if row is None:
            log.warning("[%s] round %s stamped %s, which is not in the ledger — "
                        "judging under the live catalog",
                        project.name, round_row["id"], vid)
            return project.validation
        return cv.validation_config_from_resolved(row["resolved"], project.name)

    @staticmethod
    def _validator(cfg: Any) -> Any:
        """How a round is judged: the validation panel, or nothing"""
        if not cfg.enabled:
            return None
        from . import validation

        return lambda store, round_row, packet: validation.decide(
            store, round_row, packet, cfg)

    # -- 3c. the same machine, one level up: feature orders

    def feature_validation_tick(self, project: ProjectSpec, store: ProjectStore,
                                state: fleet.Fleet | None = None) -> None:
        """Judge every FEATURE order parked in `validating`, off this thread"""
        from . import ops

        if state is not None and state.shut():
            # Recorded on the manager's timeline, exactly as `validation_tick` records it on the work order's (#714)
            self._hold_feature_rounds_for_outage(project, store, state)
            return
        for fo in store.list_feature_orders(statuses=("validating",)):
            fo_id = fo["id"]
            if fo_id in self.validating:
                continue  # its round is in flight; a second tick must not start another
            if state is not None and state.pause is not None \
                    and not state.pause.allows(fo_id):
                continue  # the user paused the fleet (#843)
            round_row = store.latest_validation_round(fo_id=fo_id)
            if round_row is None:
                # In `validating` with no round at all: nothing this machine can judge
                log.warning("[%s] feature %s is validating with no round on record",
                            project.name, fo_id)
                continue
            if round_row["outcome"] not in ("pending", "failed"):
                continue  # already judged — settlement is what moves it, not a re-run
            if validation_hold_until(
                    ops.feature_events_of_kind(store, fo_id, "validation_failed"),
                    int(round_row["round"])) > time.time():
                continue  # this round met a window of its own (`_feature_held`)
            self.validating.add(fo_id)
            future = self.validate_pool.submit(
                self._validate_feature, project, fo_id, int(round_row["id"]))
            future.add_done_callback(
                lambda f, k=fo_id: self.validating.discard(k))

    def _validate_feature(self, project: ProjectSpec, fo_id: str,
                          round_id: int) -> None:
        """One feature round, start to finish (runs on the single validate thread)"""
        from . import evidence as evidence_mod
        from . import ops

        store = ProjectStore(project.path)  # thread-local connection — see the docstring
        try:
            fo = store.get_feature_order(fo_id)
            round_row = store.get_validation_round(round_id)
            if round_row is None:  # pragma: no cover - deleted mid-flight
                return
            cfg = self._round_config(project, round_row)
            n, max_rounds = int(round_row["round"]), int(cfg.max_rounds)
            # `side_effects` is NOT passed here and is not missing: `ops` fills it
            packet = ops.collect_feature_evidence(
                store, project.path, fo, declared=str(round_row["evidence"] or ""),
                summary=str(round_row["summary"] or ""), cfg=cfg,
                history=ops.prior_round_history(store, fo_id=fo_id, before=n))
            # WHICH COMMIT THIS ROUND IS JUDGING (docs/superpowers/specs/2026-10-07-a-feature-round-must-judge-a-head-that-contains-its-children.md)
            store.set_validation_head(round_id, evidence_mod.judged_head(packet))

            validator = (self.validator if self.validator is not None
                         else self._validator(cfg))
            if validator is None:
                # Nothing to judge with — the panel is not wired in
                store.settle_validation_round(
                    round_id, "failed",
                    {"round": n, "cause": "no_validator",
                     "reason": NO_VALIDATOR_REASON, "feature_order": fo_id},
                    fo_id=fo_id, reason=NO_VALIDATOR_REASON)
                self._complete_feature(store, fo)
                log.info("[%s] feature %s: round %d settled unjudged (no validator)",
                         project.name, fo_id, n)
                return

            # NO MANAGER, NO LOOP
            if store.manager_work_order(fo_id) is None:
                self._escalate_feature(
                    store, fo, round_id, n,
                    "this feature order has no project manager work order, so a review "
                    "that asked for changes would have nobody to act on it. Nobody has "
                    "judged the work.")
                return
            # THIS GUARD DOES NOT YIELD TO SIDE EFFECTS, and the one below does
            if not packet.base:
                self._escalate_feature(
                    store, fo, round_id, n,
                    "this feature order has no recorded base commit, so there is no "
                    "honest way to say what it changed. It was released before the OS "
                    "started recording one. Nobody has judged the work.")
                return
            # The SAME helper the work-order loop calls
            empty = evidence_mod.nothing_to_judge(packet)
            if empty == "escalate":
                self._escalate_feature(
                    store, fo, round_id, n,
                    "nothing has changed on the default branch since this feature "
                    "started and its children record no other durable effect, so there "
                    "is nothing to review. Nobody has judged the work.")
                return
            if empty == "void":
                self._void_feature(store, fo, round_id, n,
                                   evidence_mod.void_reason(packet))
                log.info("[%s] feature %s: round %d voided — nothing for a reviewer "
                         "to judge", project.name, fo_id, n)
                return

            previous = self._preceding_round(store, n, fo_id=fo_id)
            if self._repeat_submission(round_row, previous):
                self._escalate_feature(
                    store, fo, round_id, n,
                    f"this submission is identical to round {previous['round']} — the "
                    f"same integrated diff and the same declared evidence — so the "
                    f"review has nothing new to judge.")
                return

            try:
                verdict = validator(store, dict(round_row), packet)
            except claude_cli.UsageLimitError as e:
                # Held, not spent — the work-order twin's reasoning verbatim (`_validation_held`)
                self._feature_held(store, fo, round_id, n, e.limit)
                log.info("[%s] feature %s: round %d held until the window reopens",
                         project.name, fo_id, n)
                return
            except claude_cli.AuthFailureError as e:
                # BEFORE the generic clause — a `ClaudeCliError` subclass, so Python would never reach it after (#778)
                self._feature_auth_held(store, fo, round_id, n, e.auth)
                log.info("[%s] feature %s: round %d held until Claude Code can "
                         "authenticate", project.name, fo_id, n)
                return
            except claude_cli.PromptTooLargeError as e:
                # BEFORE the generic clause — a `ClaudeCliError` subclass
                self._feature_refused(store, project, fo, round_id, n, e)
                return
            except claude_cli.ClaudeCliError as e:
                self._feature_outage(store, fo, round_id, n, e)
                return

            for seat in verdict.get("seats") or ():
                store.record_validation_opinion(
                    round_id, str(seat.get("seat") or ""),
                    reply=str(seat.get("reply") or ""),
                    verdict=str(seat.get("verdict") or ""),
                    status=str(seat.get("status") or "ok"),
                    model=str(seat.get("model") or ""),
                    latency_ms=int(seat.get("latency_ms") or 0))
            # `_validate_work_order`'s lines
            follow_ups = verdict.get("follow_ups") or ()
            outcome = str(verdict.get("outcome") or "")
            reason = str(verdict.get("reason") or "")

            if outcome == "passed":
                store.settle_validation_round(
                    round_id, "passed",
                    {"round": n, "round_id": round_id, "feature_order": fo_id},
                    fo_id=fo_id, reason=reason)
                self._file_follow_ups(store, project, round_row, follow_ups, cfg,
                                      unit=fo_id, fo_id=fo_id)
                self._complete_feature(store, fo)
                log.info("[%s] feature %s passed review in round %d",
                         project.name, fo_id, n)
            elif outcome == "rejected" and n < max_rounds:
                self._reject_feature(store, fo, round_id, n, max_rounds, reason,
                                     note=ops.follow_up_feedback(follow_ups))
                log.info("[%s] feature %s rejected in round %d of %d",
                         project.name, fo_id, n, max_rounds)
            elif outcome == "rejected":
                # The last round it had, and it was refused
                self._file_follow_ups(store, project, round_row, follow_ups, cfg,
                                      unit=fo_id, fo_id=fo_id)
                self._escalate_feature(store, fo, round_id, n, reason or (
                    f"the review was not satisfied after {max_rounds} rounds."))
            else:
                self._escalate_feature(store, fo, round_id, n, reason or (
                    "the review could not reach a verdict."))
        except Exception:  # noqa: BLE001 — a round must never kill the daemon
            log.exception("[%s] validating feature %s failed", project.name, fo_id)
        finally:
            store.close()

    @staticmethod
    def _reject_feature(store: ProjectStore, fo: dict, round_id: int, n: int,
                        max_rounds: int, reason: str, *, note: str = "") -> None:
        """Close the round, tell the role `manager`, and put the feature back to work"""
        from . import ops

        fo_id = fo["id"]
        store.settle_validation_round(
            round_id, "rejected",
            {"round": n, "round_id": round_id, "of": max_rounds,
             "feature_order": fo_id},
            fo_id=fo_id, reason=reason)
        bus.post(store, subject=bus.Subject(fo_id=fo_id),
                 from_role="reviewer", to_role="manager",
                 payload=bus.ReviewFeedback(
                     round=n, outcome="rejected",
                     reason=FEATURE_REVIEW_FEEDBACK.format(n=n, max=max_rounds,
                                                           reason=reason + note,
                                                           fo_id=fo_id)))
        store.set_feature_status(fo_id, "executing")
        store.clear_feature_attention(fo_id)

    def _void_feature(self, store: ProjectStore, fo: dict, round_id: int, n: int,
                      reason: str) -> None:
        """`_void` one level up: close the round and complete the feature, silently"""
        from . import ops

        fo_id = fo["id"]
        store.settle_validation_round(
            round_id, "void",
            {"round": n, "round_id": round_id, "reason": reason,
             "feature_order": fo_id},
            fo_id=fo_id, reason=reason)
        self._complete_feature(store, fo)

    @staticmethod
    def _escalate_feature(store: ProjectStore, fo: dict, round_id: int, n: int,
                          reason: str) -> None:
        """Give up on this feature and ask the user (#199)"""
        from . import ops
        from .invariants import VALIDATION_STUCK_BLOCKER

        fo_id = fo["id"]
        store.settle_validation_round(
            round_id, "escalated",
            {"round": n, "round_id": round_id, "reason": reason,
             "feature_order": fo_id},
            fo_id=fo_id, reason=reason)
        store.flag_feature_attention(fo_id, VALIDATION_STUCK_BLOCKER)
        store.add_notification(
            title=VALIDATION_ESCALATED_TITLE.format(unit=fo_id, n=n),
            body=escalation_body(reason), level="warning", source="validation",
        )

    @staticmethod
    def _feature_held(store: ProjectStore, fo: dict, round_id: int, n: int,
                      limit: claude_cli.UsageLimit) -> None:
        """`_validation_held` for a feature round — read that one; this is its twin"""
        from . import ops
        from .invariants import usage_hold_note

        fo_id = fo["id"]
        reopens = limit.reset_at or (db.now()
                                     + worker_session.RATE_LIMIT_FALLBACK_DELAY)
        store.settle_validation_round(
            round_id, "failed",
            {"round": n, "cause": VALIDATION_HELD_CAUSE, "reopens_at": reopens,
             "error": limit.message[:500], "feature_order": fo_id},
            fo_id=fo_id, reason=usage_hold_note(reopens),
            hold_cause=VALIDATION_HELD_CAUSE)

    def _feature_auth_held(self, store: ProjectStore, fo: dict, round_id: int, n: int,
                           auth: claude_cli.AuthFailure) -> None:
        """`_validation_auth_held` for a feature round — read that one; this is its twin"""
        from . import ops
        from .invariants import AUTH_HOLD_NOTE

        fo_id = fo["id"]
        attempt = 1 + sum(
            1 for e in ops.feature_events_of_kind(store, fo_id, "validation_failed")
            if db.from_json(e["payload"], {}).get("round") == n
            and db.from_json(e["payload"], {}).get("cause") == VALIDATION_AUTH_CAUSE)
        reopens = time.time() + AUTH_HOLD_BACKOFF[min(attempt - 1,
                                                      len(AUTH_HOLD_BACKOFF) - 1)]
        store.settle_validation_round(
            round_id, "failed",
            {"round": n, "cause": VALIDATION_AUTH_CAUSE, "reopens_at": reopens,
             "attempt": attempt, "error": auth.message[:500],
             "feature_order": fo_id},
            fo_id=fo_id, reason=AUTH_HOLD_NOTE, hold_cause=VALIDATION_AUTH_CAUSE)

    def _feature_refused(self, store: ProjectStore, project: ProjectSpec, fo: dict,
                         round_id: int, n: int,
                         error: claude_cli.PromptTooLargeError) -> None:
        """`_validation_refused` for a feature round — read that one; this is its twin"""
        from . import ops
        from .project_store import OS_PROMPT_REFUSED_EVENT

        fo_id = fo["id"]
        said = self.prompt_refusal_payload(f"feature validation round {n}", error)
        ops.feature_event(store, fo_id, OS_PROMPT_REFUSED_EVENT,
                          {**said, "round": n, "feature_order": fo_id})
        central = CentralStore()
        try:
            central.add_inbox(
                project=project.name, level="critical",
                title=f"An OS model call for {fo_id} was refused: the prompt was too "
                      f"large",
                body=f"Call: {said['call']}\nPrompt {said['prompt_chars']} chars + "
                     f"system prompt {said['system_prompt_chars']} chars = "
                     f"{said['total']}, over the {said['ceiling']}-char ceiling "
                     f"({said['setting']}).\nNOBODY HAS JUDGED THIS: the call was "
                     f"never made, and no retry can help.\nRaise {said['setting']} in "
                     f"the catalog, or shrink what that call carries.")
        finally:
            central.close()
        self._escalate_feature(
            store, fo, round_id, n,
            f"the review could not be run: the OS built a {said['total']}-char prompt "
            f"for it, over the {said['ceiling']}-char ceiling ({said['setting']}), so "
            f"it was never sent. Nobody has judged the work.")

    def _feature_outage(self, store: ProjectStore, fo: dict, round_id: int, n: int,
                        error: Exception) -> None:
        """The validator could not be reached"""
        from . import ops

        fo_id = fo["id"]
        outages = 1 + sum(
            1 for e in ops.feature_events_of_kind(store, fo_id, "validation_failed")
            if db.from_json(e["payload"], {}).get("round") == n
            and db.from_json(e["payload"], {}).get("cause") == "transport")
        recorded = store.settle_validation_round(
            round_id, "failed",
            {"round": n, "cause": "transport", "attempt": outages,
             "error": str(error)[:500], "feature_order": fo_id},
            fo_id=fo_id, reason=f"the validator could not be reached: {error}")
        if outages >= VALIDATION_OUTAGE_LIMIT or not recorded:
            self._escalate_feature(
                store, fo, round_id, n,
                f"the review could not be run: the validator was unreachable "
                f"{outages} times in a row. Nobody has judged the work.")

    def _compacted_first(self, project: ProjectSpec, store: ProjectStore,
                         wo: dict) -> bool:
        """Spend this work order's turn on a `/compact` instead of on the message?"""
        try:
            turn = worker_session.compact_before_relaunch(
                store, project, wo, self.catalog.os.compact_min_context)
            if turn is None:
                return False
        except budget_mod.BudgetExhausted:
            return False  # `_deliver` raises and escalates on the same call; let it
        except Exception:  # noqa: BLE001 — a saving must never cost a delivery
            log.exception("[%s] could not compact %s before its next prompt",
                          project.name, wo["id"])
            return False
        # The reason is on the `compacting` event `worker_session.compact` wrote
        log.info("[%s] compacting %s before its next prompt (turn %s)",
                 project.name, wo["id"], turn["seq"])
        return True

    def _deliver(self, project: ProjectSpec, store: ProjectStore, wo: dict,
                 msgs: list[dict[str, Any]]) -> bool:
        """True when a turn was launched — the delivery itself, or a compaction first"""
        ids = [m["id"] for m in msgs]
        anchor = ids[0]  # the ASK this turn is charged to — see the note below
        # COMPACT FIRST IF THE CACHE HAS GONE
        if self._compacted_first(project, store, wo):
            return True
        # WHY THE LAST TURN STALLED, CARRIED INTO THIS ONE (docs/superpowers/specs/2026-09-22-a-dead-background-job-is-not-a-live-one.md)
        note = background.resume_note(store, wo, msgs)
        if note:
            msgs = [{"id": store.queue_message(wo["id"], note,
                                               source=background.SOURCE),
                     "content": note, "source": background.SOURCE}, *msgs]
            # NOT `ids[0]`, which the turn is anchored to below: the cost of a turn is charged to the ASK
            ids = [m["id"] for m in msgs]
        log.info("[%s] delivering message(s) %s to %s", project.name, ids, wo["id"])
        store.add_event(wo["id"], "delivering", {"msg_ids": ids})
        # A blank line between messages and nothing else
        text = "\n\n".join(m["content"] for m in msgs)
        try:
            turn = worker_session.send(store, project, wo, text, msg_id=anchor)
        except budget_mod.BudgetExhausted as e:
            # The messages stay QUEUED, not `failed`: nothing is wrong with them and the user raising the budget...
            if not budget_mod.escalate(store, wo, e.exhausted):
                # ...but only PARKING forms that hold, so an order it declines retries for ever (kn-1e43fe1a)
                self._unaffordable(project, store, wo, ids, e.exhausted)
            return False
        except claude_cli.ClaudeCliError as e:
            # THE USER'S WORDS ARE NOT THE OS'S TO DROP (docs/superpowers/specs/2026-09-18-a-failure-is-not-an-answer.md §6.)
            outcomes = {store.record_delivery_failure(msg_id, str(e)) for msg_id in ids}
            log.error("[%s] delivery of message(s) %s failed (%s): %s", project.name,
                      ids, "/".join(sorted(outcomes)), e)
            store.add_event(wo["id"], "delivery_failed",
                            {"msg_ids": ids, "error": str(e)[:500],
                             "outcome": "failed" if "failed" in outcomes else "queued"})
            if "failed" in outcomes:
                store.flag_attention(
                    wo["id"],
                    f"a message could not be delivered to this work order after "
                    f"{MAX_MESSAGE_DELIVERY_ATTEMPTS} attempts — the worker never "
                    f"received it: {e}")
            return False
        # Every message in the turn is delivered
        for msg_id in ids:
            store.mark_message(msg_id, "delivered")
            self._stamp_objection_delivery(store, msg_id)
        store.add_event(wo["id"], "message_delivered",
                        {"msg_ids": ids, "turn": turn["seq"]})
        # The work order is moving again, whatever it had settled into
        if wo["status"] != "running":
            store.set_status(wo["id"], "running")
            store.clear_attention(wo["id"])
        return True

    def _unaffordable(self, project: ProjectSpec, store: ProjectStore, wo: dict,
                      ids: list[Any], out: budget_mod.Exhaustion) -> None:
        """Settle messages the budget refuses on an order `budget.escalate` will not park (issue #1195)"""
        for msg_id in ids:
            store.record_delivery_failure(msg_id, out.reason, max_attempts=1)
        log.warning("[%s] message(s) %s to %s are unfunded and nothing parked it: %s",
                    project.name, ids, wo["id"], out.reason)
        store.add_event(wo["id"], "delivery_failed",
                        {"msg_ids": ids, "error": out.reason[:500], "outcome": "failed"})
        # Not `out.reason`, which offers a raise: `ops.set_work_order_budget` refuses a settled order
        advice = ("a settled order has nothing left to spend, so file a new work order if "
                  "the feedback still matters"
                  if str(wo["status"] or "") in TERMINAL_STATUSES
                  else "raise its budget with `jarvis wo budget` and send the feedback again")
        c = out.ceiling
        store.add_notification(
            title=f"{wo['id']} never received {len(ids)} message(s)",
            body=(f"{wo['id']} is {wo['status']} and has spent ${c.spent_usd:.2f} of its "
                  f"${c.cap_usd:.2f} budget, so the turn that would have carried them was "
                  f"refused — {advice}."),
            level="warning", wo_id=wo["id"], source="reconciler")

    # -- 5

    def morpheus_tick(self) -> None:
        """Kick one Morpheus pass when none is running"""
        if (not self.catalog.os.morpheus.enabled or self.morpheus_busy
                or self.tick_count % MORPHEUS_EVERY_TICKS != 1):
            return
        self.morpheus_busy = True
        future = self.morpheus_pool.submit(self._morpheus_pass)
        future.add_done_callback(lambda f: setattr(self, "morpheus_busy", False))

    def _morpheus_pass(self) -> None:
        """Queue what reached production, run due scripts, hand off verdicts, then wake the session"""
        from . import morpheus
        from .paths import production_code_dir

        central = CentralStore()  # thread-local
        try:
            cfg = self.catalog.os.morpheus
            prod = production_code_dir()
            os_name = self._os_project()
            # Where each project's production lives: the OS runs from its deployed checkout
            dirs = {p.name: p.path for p in self.catalog.projects if p.path.is_dir()}
            if os_name and prod.is_dir():
                dirs[os_name] = prod
            # The OS ships by release; every other project ships when its PR merges
            if os_name and (prod / ".git").exists():
                try:
                    morpheus.ensure_release_queued(central, cfg, prod, project=os_name)
                except Exception:  # noqa: BLE001 — the rest of the pass still runs
                    log.exception("morpheus: could not queue the running release")
            for project in self.catalog.projects:
                if project.name == os_name or not project.path.is_dir():
                    continue
                store = ProjectStore(project.path)  # thread-local
                try:
                    morpheus.queue_merges(central, cfg, project.name, store)
                except Exception:  # noqa: BLE001
                    log.exception("[%s] morpheus: could not queue merged PRs", project.name)
                finally:
                    store.close()
            try:
                from .ops import fix_carrier

                for item_id in morpheus.requeue_fixed(central, cfg, fix_carrier):
                    log.info("morpheus %s: its fix reached production, watching again", item_id)
            except Exception:  # noqa: BLE001 — the rest of the pass still runs
                log.exception("morpheus: could not requeue fixed items")
            fallback = next(iter(dirs.values()), Path.cwd())
            # Else the fallback runs an archived project's scripts in another checkout (§6.2 of the offboard spec)
            skip = archived_projects(central)
            moved = morpheus.sweep(central, cfg,
                                   lambda item: dirs.get(item.get("project") or "", fallback),
                                   skip=skip)
            if any(moved.values()):
                log.info("morpheus sweep: %s", {k: v for k, v in moved.items() if v})
            for item in morpheus.owed_actions(central):
                if (item["project"] or "") in skip:
                    continue
                try:
                    log.info("morpheus %s (PR #%s) %s -> %s", item["id"], item["pr_number"],
                             item["state"], morpheus.act(central, self.catalog, item))
                except Exception:  # noqa: BLE001 — one item must not starve the others
                    log.exception("morpheus: action for %s failed", item["id"])
            # The session lives in the OS project's checkout; each batch adds the projects it touches
            home = next((p.path for p in self.catalog.projects if p.name == os_name), fallback)
            morpheus.wake(central, self.catalog, home, dirs=dirs,
                          paused=morpheus.pause_state(central)["paused"], skip=skip)
        except Exception:  # noqa: BLE001 — the loop must survive anything
            log.exception("morpheus pass failed")
        finally:
            central.close()

    def neo_tick(self) -> None:
        """Kick a queue drain when questions are waiting and none is running"""
        if not self.catalog.os.neo.enabled or self.neo_draining:
            return
        from .neo_store import NeoStore
        store = NeoStore()
        try:
            stale = store.reclaim_stale()
            if stale["requeued"] or stale["failed"]:
                log.warning("neo reclaimed stranded questions: requeued=%s failed=%s",
                            stale["requeued"], stale["failed"])
            if stale["failed"]:
                self._note_stranded_unreachable(store, stale["failed"])
            queued = store.counts().get("queued", 0)
        finally:
            store.close()
        if not queued:
            return
        self.neo_draining = True
        future = self.neo_pool.submit(self._neo_drain)
        future.add_done_callback(lambda f: setattr(self, "neo_draining", False))

    def _note_stranded_unreachable(self, store: Any, failed: list[int]) -> None:
        """Tell the user about the questions `reclaim_stale` just gave up on (§5)"""
        from .neo_store import UNREACHABLE_PREFIX

        central = CentralStore()
        try:
            for qid in failed:
                try:
                    q = store.get(qid)
                    if not q:
                        continue
                    detail = str(q["answer_reason"] or "")
                    if detail.startswith(UNREACHABLE_PREFIX):
                        detail = detail[len(UNREACHABLE_PREFIX):]
                    self._note_question_unreachable(central, q, detail)
                except Exception:  # noqa: BLE001
                    log.exception("could not surface stranded neo question %s", qid)
        finally:
            central.close()

    def _note_question_unreachable(self, central: CentralStore, q: dict,
                                   detail: str) -> None:
        """Neo's retries are spent"""
        from . import invariants

        if q.get("kind") == "findings":
            # The analyst behind it has settled: the finding, on its io, is what the user decides
            self._findings_question_unanswered(q, f"Neo could not be reached: {detail}")
            return
        head = q["question"].strip().splitlines()[0][:200]
        central.add_inbox(
            project=q["project"], level="warning",
            title=f"Neo could not be reached for a question from {q['wo_id']}",
            body=f"Q: {head}\nNOBODY HAS JUDGED THIS — Neo's model call failed "
                 f"every time it was tried. This is not an escalation: Neo made no "
                 f"decision.\nLast error: {detail[:200]}\n"
                 f"Read it and answer it: "
                 f"{notify.neo_question_url(self.catalog, q['id'])}\n"
                 f"Or from a terminal: jarvis neo answer {q['id']} \"...\"",
            wo_id=q["wo_id"],
        )
        ppath = {p.name: p.path for p in self.catalog.projects}.get(q["project"])
        if ppath and ppath.is_dir():
            pstore = ProjectStore(ppath)
            try:
                pstore.flag_attention(
                    q["wo_id"],
                    invariants.neo_question_blocker({**q, "status": "failed"}))
            finally:
                pstore.close()

    @staticmethod
    def prompt_refusal_payload(
            call: str,
            error: claude_cli.PromptTooLargeError | QuestionTooLargeError,
    ) -> dict[str, Any]:
        """What a refusal is allowed to say about itself (docs/superpowers/specs/2026-09-26-bounded-model-inputs.md)"""
        prompt_chars = int(getattr(error, "prompt_chars", 0)
                           or getattr(error, "question_chars", 0))
        system_prompt_chars = int(getattr(error, "system_prompt_chars", 0)
                                  or getattr(error, "context_chars", 0))
        return {"call": call, "prompt_chars": prompt_chars,
                "system_prompt_chars": system_prompt_chars,
                "total": prompt_chars + system_prompt_chars,
                "ceiling": error.ceiling, "setting": "os.max_os_prompt_chars"}

    def _note_prompt_refusal(self, central: CentralStore, store: ProjectStore,
                             project: str, wo_id: str, call: str,
                             error: claude_cli.PromptTooLargeError
                             | QuestionTooLargeError) -> dict[str, Any]:
        """The OS refused its own prompt"""
        from . import invariants
        from .project_store import OS_PROMPT_REFUSED_EVENT

        said = self.prompt_refusal_payload(call, error)
        store.add_event(wo_id, OS_PROMPT_REFUSED_EVENT, said)
        central.add_inbox(
            project=project, level="critical",
            title=f"An OS model call for {wo_id} was refused: the prompt was too large",
            body=f"Call: {said['call']}\n"
                 f"Prompt {said['prompt_chars']} chars + system prompt "
                 f"{said['system_prompt_chars']} chars = {said['total']}, over the "
                 f"{said['ceiling']}-char ceiling ({said['setting']}).\n"
                 f"NOBODY HAS JUDGED THIS: the call was never made, so nothing was "
                 f"decided and no retry can help — the same prompt is built the same "
                 f"way every time.\n"
                 f"Raise {said['setting']} in the catalog, or shrink what that call "
                 f"carries.",
            wo_id=wo_id)
        store.flag_attention(wo_id, invariants.os_prompt_refused_blocker(said))
        return said

    def _note_confirmation_refused(self, project: ProjectSpec, store: ProjectStore,
                                   wo: dict, assumption: dict,
                                   error: QuestionTooLargeError) -> None:
        """A confirmation question the store would not hold (docs/superpowers/specs/2026-09-26-bounded-model-inputs.md)"""
        if store.os_prompt_refusal_open(wo["id"]) is not None:
            log.info("[%s] %s: assumption #%s is still too large to confirm: %s",
                     project.name, wo["id"], assumption.get("n"), error)
            return
        central = CentralStore()          # thread-local, as `_validation_refused` opens
        try:
            self._note_prompt_refusal(
                central, store, project.name, wo["id"],
                f"assumption #{assumption.get('n')} confirmation", error)
        finally:
            central.close()

    def _neo_drain(self) -> None:
        """Answer every queued question in order (runs on the single neo thread)"""
        from . import invariants
        from . import neo as neo_mod
        from .neo_store import NeoStore

        store = NeoStore()  # thread-local connection
        central = CentralStore()
        paths = {p.name: p.path for p in self.catalog.projects}
        cfg = self.catalog.os.neo

        def deliver(q: dict, verdict: dict) -> None:
            ppath = paths.get(q["project"])
            pstore = ProjectStore(ppath) if ppath and ppath.is_dir() else None
            try:
                if q.get("kind") == "triage":
                    # FIRST, and it returns: this is the one kind with no work order behind it, so every branch below (#240)
                    self._deliver_triage_verdict(central, q, verdict)
                    return
                if q.get("kind") == "validation":
                    # Triage's twin: Morpheus asked, and no work order stands behind it
                    from . import morpheus
                    morpheus.record_neo_verdict(
                        central, self.catalog.os.morpheus, q, verdict,
                        notify.neo_question_url(self.catalog, q["id"]))
                    return
                if q.get("kind") == "approval":
                    self._deliver_gate_verdict(central, pstore, q, verdict)
                elif q.get("kind") == "plan":
                    self._deliver_plan_verdict(central, store, pstore, q, verdict)
                elif q.get("kind") == "findings":
                    # Owns its escalation too: the finding, not a worker, is what waits
                    self._deliver_findings_verdict(q, verdict)
                elif q.get("kind") == "alarm":
                    # ABOVE the escalate branch, because this kind owns both outcomes
                    self._deliver_alarm_verdict(central, pstore, q, verdict)
                elif q.get("kind") == "assumption":
                    # Above it for the alarm's reason
                    self._deliver_assumption_verdict(central, store, pstore, q, verdict)
                elif verdict.get("refine"):
                    if pstore:
                        # Delivered like an answer, so the wait the question started ends
                        pstore.queue_message(
                            q["wo_id"], f"{neo_mod.REFINE_PREFIX} {verdict['reason']}",
                            source="neo")
                        pstore.add_event(q["wo_id"], "neo_refined",
                                         {"neo_question_id": q["id"],
                                          "reason": verdict["reason"]})
                        invariants.end_wait_if_nothing_is_out(pstore, q["wo_id"])
                elif verdict["escalate"]:
                    # A headline, never the verbatim question: this row's job is to get the user's attention (#67)
                    head = q["question"].strip().splitlines()[0][:200]
                    central.add_inbox(
                        project=q["project"], level="warning",
                        title=f"Neo escalated a question from {q['wo_id']}",
                        body=f"Q: {head}\nWhy: {(verdict['reason'] or '')[:200]}\n"
                             f"Read it and answer it: "
                             f"{notify.neo_question_url(self.catalog, q['id'])}\n"
                             f"Or from a terminal: jarvis neo answer {q['id']} \"...\"",
                        wo_id=q["wo_id"],
                    )
                    if pstore:
                        # The reason `invariants.true_blockers` re-derives for this question
                        pstore.flag_attention(
                            q["wo_id"],
                            invariants.neo_question_blocker(
                                {**q, "status": "failed" if verdict.get("failed")
                                                else "escalated"}),
                        )
                elif pstore:
                    pstore.queue_message(
                        q["wo_id"], f"{neo_mod.ANSWER_PREFIX} {verdict['answer']}",
                        source="neo",
                    )
                    pstore.add_event(q["wo_id"], "neo_answered",
                                     {"neo_question_id": q["id"]})
                    # End the wait the question started, exactly as a gate verdict ends a gate's
                    invariants.end_wait_if_nothing_is_out(pstore, q["wo_id"])
                # `alarm` joins the two exclusions for a reason of its own: a cleanup work order dispatched off a...
                if pstore and q.get("kind") not in ("approval", "plan", "alarm",
                                                    "assumption", "findings"):
                    self._dispatch_neo_cleanup(pstore, q, verdict)
            finally:
                if pstore:
                    pstore.close()

        def refused(q: dict, error: claude_cli.PromptTooLargeError) -> None:
            """The OS built a prompt past its own ceiling (spec §4)"""
            if q.get("kind") == "findings":
                self._findings_question_unanswered(q, f"its prompt was too large: {error}")
                return
            ppath = paths.get(q["project"])
            if not (ppath and ppath.is_dir() and q.get("wo_id")):
                # A `triage` question has no work order behind it, so there is nothing to flag (#240)
                central.add_inbox(
                    project=q["project"], level="critical",
                    title="An OS model call was refused: the prompt was too large",
                    body=str(error), wo_id=None)
                return
            pstore = ProjectStore(ppath)
            try:
                self._note_prompt_refusal(
                    central, pstore, q["project"], q["wo_id"],
                    f"neo {q.get('kind') or 'question'}", error)
            finally:
                pstore.close()

        try:
            results = neo_mod.drain_queue(
                store, model=cfg.model, learnings_limit=cfg.learnings_limit,
                deliver=deliver, answer=self._panel_answer(cfg),
                unreachable=lambda q, detail: self._note_question_unreachable(
                    central, q, detail),
                refused=refused,
                # Answering an offboarded project's question is spend delivered to nothing (§6.2 of the offboard spec)
                skip=archived_projects(central),
            )
            if results:
                log.info("neo drained %d question(s)", len(results))
        except Exception:  # noqa: BLE001 — the drain must never kill the daemon
            log.exception("neo drain failed")
        finally:
            store.close()
            central.close()

    # -- 5b. dashboard digests (display only — see `jarvis.digest`)

    def digest_tick(self) -> None:
        """Shorten over-long questions for the `/neo` page, off the critical path"""
        cfg = self.catalog.os.neo
        if not cfg.enabled or not cfg.digest_model or self.digesting:
            return
        from . import digest as digest_mod
        from .neo_store import NeoStore

        store = NeoStore()
        try:
            pending = bool(store.questions_needing_digest(digest_mod.MIN_CHARS, limit=1))
        finally:
            store.close()
        if not pending:
            return
        self.digesting = True
        future = self.digest_pool.submit(self._digest_batch)
        future.add_done_callback(lambda f: setattr(self, "digesting", False))

    def _digest_batch(self) -> None:
        """Digest the questions waiting for one (runs on the single digest thread)"""
        from . import agent_usage
        from . import digest as digest_mod
        from .neo_store import NeoStore

        model = self.catalog.os.neo.digest_model
        store = NeoStore()  # thread-local connection
        try:
            for q in store.questions_needing_digest(digest_mod.MIN_CHARS,
                                                    limit=DIGEST_BATCH):
                try:
                    view = digest_mod.summarise(
                        q["question"], model=model,
                        max_chars=self.catalog.os.neo.digest_max_question_chars,
                        # The question knows which work order it came from, and the transport does not
                        on_usage=agent_usage.recorder(
                            "digest", project=q.get("project") or "",
                            wo_id=q.get("wo_id") or "", model=model,
                            question_id=q["id"]))
                except Exception as e:  # noqa: BLE001 — a digest is never worth a crash
                    # Recorded, not retried: see `digest.encode_failure`
                    log.warning("digest failed for neo question %s: %s", q["id"], e)
                    store.set_digest(q["id"], digest_mod.encode_failure(str(e)))
                    continue
                store.set_digest(q["id"], digest_mod.encode(view))
                log.info("digested neo question %s (%d chars)",
                         q["id"], len(q["question"]))
        except Exception:  # noqa: BLE001 — the daemon must survive anything
            log.exception("digest batch failed")
        finally:
            store.close()

    @staticmethod
    def _panel_answer(cfg: Any) -> Any:
        """How this drain answers a question: the panel, or nothing (meaning the single agent)"""
        if not cfg.panel.enabled:
            return None

        from . import neo as neo_mod
        from . import panel

        def answer(store: Any, q: dict, model: str, learnings_limit: int) -> dict:
            if (q.get("kind") or "question") in cfg.panel.kinds:
                return panel.decide(store, q, cfg)
            return neo_mod.answer_question(store, q, model, learnings_limit)

        return answer

    def _dispatch_neo_cleanup(self, pstore: ProjectStore, q: dict, verdict: dict) -> None:
        """File the pre-approved ledger cleanup Neo asked for, if it asked for one"""
        dispatch = verdict.get("dispatch")
        if not dispatch:
            return
        try:
            origin_wo = pstore.get_work_order(q["wo_id"])
        except KeyError:
            origin_wo = {}
        if origin_wo.get("origin") == "neo":
            log.info("neo cleanup dispatch from %s ignored: already a cleanup work order",
                     q["wo_id"])
            return
        from . import ops

        rider = {"question_id": q["id"], "wo_id": str(q["wo_id"]),
                 "title": str(dispatch.get("title") or ""),
                 "description": str(dispatch.get("description") or "")}
        # A ledger cleanup is `medium`; Neo's verdict is its one sighting
        decision = steward.gate(
            steward.Candidate("neo_rider", "medium", str(q.get("project") or ""),
                              rider["title"], evidence=(f"neo question {q['id']}",),
                              parent_wo_id=rider["wo_id"], payload=rider, refs=1),
            catalog=self.catalog, store=pstore,
            self_dispatch=self.self_dispatch)
        if not decision.allowed:
            log.info("neo cleanup dispatch from %s not filed: steward %s (%s)",
                     q["wo_id"], decision.outcome, decision.reason)
            return
        ops.file_neo_cleanup(pstore, rider, decision)

    def _deliver_findings_verdict(self, q: dict, verdict: dict) -> None:
        """Apply Neo's ruling on one improvement-order finding; the steward decides its spend"""
        from . import ops

        try:
            out = ops.apply_findings_verdict(q, verdict)
        except ops.OpsError:
            log.exception("neo's verdict on findings question %s could not be applied",
                          q["id"])
            return
        log.info("findings question %s: %s", q["id"],
                 out.get("reason") or out.get("status") or "applied")

    def _findings_question_unanswered(self, q: dict, detail: str) -> None:
        """Neo made no decision on a finding, so the finding is the user's"""
        from . import ops

        try:
            ops.findings_question_unanswered(q, detail)
        except ops.OpsError:
            log.exception("findings question %s: could not hand it to the user", q["id"])

    def _deliver_plan_verdict(self, central: CentralStore, neo_store: Any,
                              pstore: ProjectStore | None, q: dict,
                              verdict: dict) -> None:
        """Apply Neo's verdict on a submitted plan"""
        from . import autoreview
        from . import db as db_mod
        from . import plans

        if pstore is None:
            log.error("plan verdict for question %s has no project store", q["id"])
            return
        fo = pstore.feature_order_for_question(q["id"])
        if fo is None:
            log.warning("plan question %s reviews no feature order (deleted?)", q["id"])
            return
        if fo["status"] != "plan_review":
            # The user got there first through `jarvis fo approve`
            log.info("plan question %s: %s is already %s, dropping Neo's verdict",
                     q["id"], fo["id"], fo["status"])
            return

        plan = db_mod.from_json(fo.get("plan"), {}) or {}
        n_children = len(plan.get("children") or [])
        over_cap = n_children >= plans.CHILD_CAP
        if not verdict["escalate"] and not over_cap:
            from . import ops
            accepted = verdict.get("verdict") == "approved"
            try:
                ops.review_plan(fo["id"], accept=accepted,
                                feedback=verdict["reason"] or "(no reason given)",
                                decided_by="neo")
            except ops.OpsError:
                log.exception("neo's verdict on %s could not be applied", fo["id"])
            else:
                log.info("neo %s the plan for %s", "released" if accepted else
                         "sent back", fo["id"])
            return

        reason = verdict["reason"] or "Neo declined to decide"
        if over_cap and not verdict["escalate"]:
            reason = (f"{n_children} children is at or over the cap of "
                      f"{plans.CHILD_CAP}, so this plan needs you rather than Neo. "
                      f"Neo's reading: {verdict.get('verdict', '?')} — {reason}")
            # Neo answered, but the answer is not what happens (docs/superpowers/specs/2026-10-01-neo-observability.md)
            neo_store.mark(q["id"], "escalated", reason=reason,
                           cause=autoreview.escalation_cause(
                               over_cap=True, escalate=bool(verdict["escalate"])))
        pstore.flag_feature_attention(fo["id"], f"plan needs your review: {reason[:160]}")
        central.add_inbox(
            project=q["project"], level="warning",
            title=f"A plan needs your review: {fo['id']}",
            body=f"{fo['title']}\n{n_children} work orders proposed.\n{reason}\n\n"
                 f"Read it with: jarvis fo show {fo['id']}\n"
                 f"Then: jarvis fo approve {fo['id']} [--reject] --feedback \"...\"",
            wo_id=fo.get("plan_wo_id"),
        )
        log.info("plan for %s escalated to the user: %s", fo["id"], reason)

    def _deliver_alarm_verdict(self, central: CentralStore, pstore: ProjectStore | None,
                               q: dict, verdict: dict) -> None:
        """Apply Neo's reading of a cost alarm the supervisor could not settle — §3"""
        from . import db as db_mod, ops, supervisor

        if pstore is None:
            log.error("alarm verdict for question %s has no project store", q["id"])
            return
        alarm = pstore.alarm_for_question(q["id"])
        if alarm is None:
            log.warning("alarm question %s judges no alarm (work order deleted?)",
                        q["id"])
            return
        if alarm["status"] != "escalated":
            # The user got there first through `jarvis alarms review`, which closes the question on its way past
            log.info("alarm question %s: %s is already %s, dropping Neo's verdict",
                     q["id"], alarm["id"], alarm["status"])
            return

        if verdict["escalate"]:
            # The alarm STAYS `escalated` and is now the user's
            central.add_inbox(
                project=q["project"], level="warning",
                title=supervisor.ESCALATED_INBOX_TITLE.format(alarm_id=alarm["id"]),
                body=f"{alarm['reason']}\n"
                     f"The supervisor could not settle it: {alarm['verdict_reason']}\n"
                     f"Neo declined to decide: {(verdict['reason'] or '')[:200]}\n"
                     f"Read it with: jarvis alarms show {alarm['id']}\n"
                     "The question in full: "
                     f"{notify.neo_question_url(self.catalog, q['id'])}",
                wo_id=q["wo_id"])
            pstore.flag_attention(q["wo_id"], supervisor.ALARM_BLOCKER.format(
                alarm_id=alarm["id"]))
            log.info("alarm %s escalated to the user by neo", alarm["id"])
            return

        # The PROJECT's clip, not a literal: `note` is what the user is shown instead of an interruption and...
        cfg = self._supervisor_config(q["project"])
        note = (verdict["answer"] or "").strip()[:cfg.note_chars]
        pstore.update_alarm(
            alarm["id"], status="acked", verdict="ack", note=note,
            verdict_reason=supervisor.NEO_ANSWERED_REASON.format(
                reason=verdict["reason"] or "(no reason given)"),
            decided_at=db_mod.now())
        pstore.add_event(q["wo_id"], "alarm_advice",
                         {"alarm_id": alarm["id"], "neo_question_id": q["id"],
                          "answer": note})
        # §2's ack path exactly, `ops.ack_os_flag`: this settles the ALARM
        try:
            ops.ack_os_flag(q["wo_id"])
        except ops.OpsError as exc:
            log.info("alarm %s acked by neo; attention left up: %s", alarm["id"], exc)
        central.add_inbox(
            project=q["project"], level="info",
            title=supervisor.ADVICE_INBOX_TITLE.format(wo_id=q["wo_id"]),
            body=f"{note}\n{supervisor.ALARM_PATH.format(project=q['project'], alarm_id=alarm['id'])}",
            wo_id=q["wo_id"])
        log.info("neo answered alarm %s", alarm["id"])

    def _supervisor_config(self, project: str) -> Any:
        """This project's supervisor settings, falling back to the shipped defaults"""
        from .catalog import CatalogError, SupervisorConfig

        try:
            return self.catalog.project(project).supervisor
        except CatalogError:
            return SupervisorConfig()

    def _deliver_gate_verdict(self, central: CentralStore, pstore: ProjectStore | None,
                              q: dict, verdict: dict) -> None:
        """Apply Neo's verdict on a privileged-action request"""
        from . import gates

        if pstore is None:
            log.error("gate verdict for %s has no project store — request %s left pending",
                      q["wo_id"], q["id"])
            return
        approval = pstore.approval_for_question(q["id"])
        if approval is None:
            log.error("neo question %s is an approval with no approval row", q["id"])
            return

        if verdict["escalate"]:
            # A contest must not arrive offering `approve`, the one verb that cannot answer it
            if approval["contested"]:
                title = f"Is this a false positive? {approval['kind']} from {q['wo_id']}"
                body = (f"The worker says this command performs no privileged action and "
                        f"the `{approval['kind']}` gate matched it by mistake. Neo "
                        f"declined to decide: {verdict['reason']}\n\n"
                        f"Command: {approval['command']}\n\n"
                        f"It is a false positive:  jarvis gate dismiss {approval['id']} "
                        f"--reason \"...\"\n"
                        f"The worker is wrong:     jarvis gate deny {approval['id']} "
                        f"--reason \"...\"\n"
                        f"The contest in full:     jarvis gate show {approval['id']}")
            else:
                title = f"Approval needed: {approval['kind']} from {q['wo_id']}"
                body = (f"Neo declined to decide: {verdict['reason']}\n\n"
                        f"Command: {approval['command']}\n\n"
                        f"Approve it with: jarvis gate approve {approval['id']} "
                        f"--reason \"...\"\n"
                        f"Deny it with:    jarvis gate deny {approval['id']} "
                        f"--reason \"...\"\n"
                        f"Full request:    jarvis gate show {approval['id']}")
            central.add_inbox(project=q["project"], level="warning", title=title,
                              body=body, wo_id=q["wo_id"])
            pstore.mark_approval_escalated(approval["id"], verdict["reason"])
            pstore.flag_attention(
                q["wo_id"],
                (f"contested gate match escalated by Neo: {approval['kind']} "
                 f"(request {approval['id']})") if approval["contested"] else
                (f"gate approval escalated by Neo: {approval['kind']} "
                 f"(request {approval['id']})"),
            )
            pstore.add_event(q["wo_id"], "gate_escalated", {
                "approval_id": approval["id"], "reason": verdict["reason"],
            })
            return

        asked = verdict.get("verdict") or ("approved" if verdict.get("approve")
                                           else "denied")
        decided = gates.apply_decision(pstore, approval["id"], verdict=asked,
                                       reason=verdict["reason"], decided_by="neo",
                                       central=central, project=q["project"],
                                       exempt_pattern=verdict.get("exempt_pattern", ""))
        # WHAT WAS RECORDED, not what the reviewer said: the two differ on exactly one input
        ruling = decided["status"]
        # A shipped release is something the user wants to know happened
        if ruling != "dismissed":
            # A rejected CONTEST stays visible — the one place this feature spends the user's attention on purpose
            central.add_inbox(
                project=q["project"],
                level="info" if ruling == "approved" else "warning",
                title=(f"Neo rejected a contested {approval['kind']} match from "
                       f"{q['wo_id']}" if decided["contested"]
                       else f"Neo {ruling} {approval['kind']} for {q['wo_id']}"),
                body=(f"{decided['decision_reason']}\n\n"
                      f"Command: {approval['command']}\n"
                      f"Review Neo's call with: jarvis neo review {q['id']}"),
                wo_id=q["wo_id"],
            )
        log.info("gate %s %s by neo for %s", approval["id"], ruling, q["wo_id"])

    # -- 5c. the supervisor: answering a cost alarm (see `jarvis.supervisor`)

    def _supervised_projects(self) -> list[ProjectSpec]:
        """Projects whose supervisor is on, read from the PROJECT's resolved config"""
        return [p for p in self.catalog.projects
                if p.supervisor.enabled and p.path.is_dir()]

    def supervisor_tick(self) -> None:
        """Kick a review drain when alarms are waiting and none is running — §2"""
        supervised = self._supervised_projects()
        if not supervised or self.supervisor_draining:
            return
        waiting = 0
        for project in supervised:
            store = self.store_for(project)
            stale = store.reclaim_stale_alarms(project.supervisor.stale_reviewing_seconds,
                                               project.supervisor.max_review_attempts)
            if stale["requeued"] or stale["failed"]:
                log.warning("supervisor reclaimed stranded alarms in %s: "
                            "requeued=%s failed=%s",
                            project.name, stale["requeued"], stale["failed"])
            waiting += len(store.alarms_across(statuses=("raised",)))
        if not waiting:
            return
        self.supervisor_draining = True
        future = self.supervisor_pool.submit(self._supervisor_drain, supervised)
        future.add_done_callback(
            lambda f: setattr(self, "supervisor_draining", False))

    def remedy_tick(self) -> None:
        """Apply the remedies whose gate has since opened (docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md)"""
        supervised = [p for p in self._supervised_projects()
                      if p.supervisor.remedies.enabled]
        if not supervised or self.remedy_applying:
            return
        from . import remedies as remedies_mod

        ready: list[tuple[ProjectSpec, str | None, int | None]] = []
        for project in supervised:
            store = self.store_for(project)
            for alarm in store.alarms_across(statuses=("proposed",)):
                approval_id = alarm.get("remedy_approval_id")
                approval = store.get_approval(int(approval_id)) if approval_id else None
                if approval is not None and approval["status"] == "approved":
                    ready.append((project, alarm["id"], int(approval["id"])))
            # §11 of docs/superpowers/specs/2026-09-24-order-observability.md: a USER-INITIATED fix has no alarm...
            for grant in remedies_mod.user_grants(store):
                ready.append((project, None, int(grant["id"])))
        if not ready:
            return
        self.remedy_applying = True
        future = self.supervisor_pool.submit(self._apply_remedies, ready)
        future.add_done_callback(
            lambda f: setattr(self, "remedy_applying", False))

    def _apply_remedies(self,
                        ready: list[tuple[ProjectSpec, str | None, int | None]]) -> None:
        """Run each approved remedy on the supervisor thread"""
        from . import remedies

        central = CentralStore()
        try:
            for project, alarm_id, approval_id in ready:
                pstore = ProjectStore(project.path)
                subject_of_log = alarm_id or f"gate request {approval_id}"
                try:
                    self._apply_one_remedy(project, pstore, central, alarm_id, remedies,
                                           approval_id)
                except remedies.RemedyRefused as exc:
                    log.warning("[%s] remedy for %s refused: %s", project.name,
                                subject_of_log, exc)
                except Exception:  # noqa: BLE001 — one alarm must not stop the rest
                    log.exception("[%s] applying the remedy for %s failed",
                                  project.name, subject_of_log)
                finally:
                    pstore.close()
        finally:
            central.close()

    def _apply_one_remedy(self, project: ProjectSpec, pstore: ProjectStore,
                          central: CentralStore, alarm_id: str | None,
                          remedies: Any, approval_id: int | None = None) -> None:
        if alarm_id is None:
            # §11's alarm-less grant
            approval = pstore.get_approval(int(approval_id))  # type: ignore[arg-type]
            if approval is None:
                return
            subject = remedies.subject_of_grant(pstore, approval)
            if subject is None:
                log.warning("[%s] gate request %s names a subject that is gone",
                            project.name, approval_id)
                return
            result = remedies.apply(pstore, central, project.name, approval, None,
                                    subject)
            log.info("[%s] user fix applied on %s: %s", project.name, subject["id"],
                     result)
            return
        alarm = pstore.get_alarm(alarm_id)
        if alarm["status"] != "proposed":
            return
        approval_id = alarm.get("remedy_approval_id")
        approval = pstore.get_approval(int(approval_id)) if approval_id else None
        subject = self._alarm_subject(pstore, alarm, remedies)
        result = remedies.apply(pstore, central, project.name, approval, alarm, subject)
        log.info("[%s] remedy %s applied for %s: %s", project.name, alarm["remedy"],
                 alarm_id, result)

    def _alarm_subject(self, pstore: ProjectStore, alarm: dict,
                       remedies: Any) -> dict:
        """The work order or feature order an alarm is about — §1's two subject kinds"""
        if remedies.subject_kind(alarm) == "feature_order":
            return pstore.get_feature_order(alarm["fo_id"])
        return pstore.get_work_order(alarm["wo_id"])

    def _supervisor_drain(self, projects: list[ProjectSpec]) -> None:
        """Judge every raised alarm, project by project (on the supervisor thread)"""
        from . import supervisor as supervisor_mod
        from .neo_store import NeoStore

        neo_store = NeoStore()   # thread-local connections, as `_neo_drain` opens its own
        central = CentralStore()
        try:
            for project in projects:
                pstore = ProjectStore(project.path)
                try:
                    self._drain_project_alarms(project, pstore, neo_store, central,
                                               supervisor_mod)
                except Exception:  # noqa: BLE001 — one project must not stop the rest
                    log.exception("supervisor drain failed for %s", project.name)
                finally:
                    pstore.close()
        finally:
            neo_store.close()
            central.close()

    def _drain_project_alarms(self, project: ProjectSpec, pstore: ProjectStore,
                              neo_store: Any, central: CentralStore,
                              supervisor_mod: Any) -> None:
        """Claim and judge one project's alarms until the queue is empty — §2"""
        cfg = project.supervisor
        max_age = cfg.max_age_hours * SECONDS_PER_HOUR
        while True:
            alarm = pstore.claim_next_alarm()
            if alarm is None:
                return
            age = db.now() - float(alarm["ts"] or 0.0)
            if age > max_age:
                pstore.update_alarm(
                    alarm["id"], status="skipped", decided_at=db.now(),
                    verdict_reason=f"raised {age / SECONDS_PER_HOUR:.0f}h ago, past the "
                                   f"{cfg.max_age_hours}h review window — the spend can "
                                   f"no longer be prevented")
                continue
            try:
                wo = pstore.get_work_order(alarm["wo_id"])
            except KeyError:
                pstore.update_alarm(alarm["id"], status="skipped", decided_at=db.now(),
                                    verdict_reason="the work order is gone")
                continue
            verdict = supervisor_mod.review(
                pstore, neo_store, project.name, wo, alarm, cfg,
                central=central, inspect_cfg=project.inspect)
            log.info("[%s] alarm %s: %s (%s)", project.name, alarm["id"],
                     verdict["decision"], verdict["reason"][:cfg.reason_chars])

    # -- 5d. the health sweep: looking before anything crosses a threshold

    def _health_projects(self) -> list[ProjectSpec]:
        """Projects being swept on THIS tick (docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md)"""
        return [p for p in self.catalog.projects
                if p.supervisor.enabled and p.supervisor.health_enabled
                and p.path.is_dir()
                and self.tick_count % p.supervisor.health_every_ticks == 1]

    def health_tick(self, state: fleet.Fleet | None = None) -> None:
        """Kick a sweep when a unit is due and none is running"""
        due = self._health_projects()
        if not due or self.health_sweeping:
            return
        self.health_sweeping = True
        future = self.health_pool.submit(self._health_sweep, due, state)
        future.add_done_callback(lambda f: setattr(self, "health_sweeping", False))

    def _health_candidates(self, pstore: ProjectStore,
                           cfg: Any) -> list[tuple[float, dict, str]]:
        """Every open unit due for a look, LONGEST-UNREVIEWED FIRST"""
        from . import health

        now = db.now()
        subjects = [{"kind": "work_order", "row": wo}
                    for wo in pstore.list_work_orders(statuses=OPEN_STATUSES)
                    if wo["origin"] not in UNGOVERNED_ORIGINS]
        subjects += [{"kind": "feature_order", "row": fo}
                     for fo in pstore.list_feature_orders(statuses=FO_OPEN_STATUSES)
                     # A feature with no carrier has no session at all and nothing to record a finding on
                     if pstore.carrier_for_feature(fo["id"]) is not None]
        out = []
        for subject in subjects:
            row = subject["row"]
            last = pstore.last_health_review(subject["kind"], row["id"])
            # TWO READS, and they disagree precisely when the sweep is failing: `last` is the last JUDGEMENT and... (#216)
            attempt = pstore.last_health_attempt_ts(subject["kind"], row["id"])
            # The blocker is computed BESIDE the fingerprint and passed in: `due` is the whole spend decision and...
            trigger = health.due(last, health.fingerprint(pstore, subject), cfg, now,
                                 float(row.get("created_at") or 0.0),
                                 last_attempt=attempt,
                                 blocker=health.blocker(pstore, subject, cfg))
            if trigger:
                out.append((float(last["ts"]) if last else 0.0, subject, trigger))
        out.sort(key=lambda c: c[0])
        return out

    def _health_sweep(self, projects: list[ProjectSpec],
                      state: fleet.Fleet | None = None) -> None:
        """Sweep each project's due units, capped (on the health thread) (docs/superpowers/specs/2026-09-28-a-usage-limit-is-not-a-failed-sweep.md §3)"""
        from . import health
        from . import supervisor as supervisor_mod
        from .neo_store import NeoStore

        neo_store = NeoStore()   # thread-local connections, as `_supervisor_drain` does
        try:
            for project in projects:
                cfg = project.supervisor
                pstore = ProjectStore(project.path)
                try:
                    if state is not None and state.shut():
                        self._hold_sweep_for_outage(project, pstore, state)
                        continue
                    hold = pstore.health_sweep_hold()
                    if hold is not None:
                        # Nothing is swept, no candidates are derived
                        log.debug("[%s] health sweep held until %s: %s",
                                  project.name, hold[0], hold[1])
                        continue
                    candidates = self._health_candidates(pstore, cfg)
                    # THE CAP BOUNDS SPEND, so it bounds the PAID triggers only and every due re-assertion is processed
                    free = [c for c in candidates if c[2] == health.REASSERT]
                    paid = [c for c in candidates
                            if c[2] != health.REASSERT][:cfg.health_max_units_per_tick]
                    for _, subject, trigger in free + paid:
                        if trigger == health.REASSERT:
                            supervisor_mod.reassert_health(pstore, project.name, subject)
                            continue
                        supervisor_mod.review_health(
                            pstore, neo_store, project.name, subject, cfg.probes, cfg,
                            trigger, inspect_cfg=project.inspect)
                except Exception:  # noqa: BLE001 — one project must not stop the rest
                    log.exception("health sweep failed for %s", project.name)
                finally:
                    pstore.close()
        finally:
            neo_store.close()

    @staticmethod
    def _hold_sweep_for_outage(project: ProjectSpec, pstore: ProjectStore,
                               state: fleet.Fleet) -> None:
        """Write down that the ACCOUNT's window — not this project — is why nobody swept"""
        reopens = state.outage.reopens_at  # type: ignore[union-attr]
        hold = pstore.health_sweep_hold()
        if hold is not None and hold[0] >= reopens:
            return
        pstore.record_health_review(
            "account", "", fingerprint="", trigger="account-window", outcome="held",
            detail=state.outage.message[:500],  # type: ignore[union-attr]
            reopens_at=reopens)
        log.info("[%s] health sweep held by the account's usage window until %s",
                 project.name, reopens)

    # -- 5e. the stuck sweep: an order that stops moving gets investigated

    #: The run row, newest only
    STUCK_RUN_KEY = "stuck_sweep_run"

    def stuck_cadence(self) -> int:
        """Ticks between two entries into `stuck_tick`"""
        from .catalog import DEFAULT_FLEET_HEALTH_SWEEP_EVERY_TICKS

        asked = [p.fleet_health.sweep_every_ticks for p in self.catalog.projects
                 if p.fleet_health.enabled]
        return min(asked) if asked else DEFAULT_FLEET_HEALTH_SWEEP_EVERY_TICKS

    def _stuck_projects(self) -> list[ProjectSpec]:
        """Every project the stuck sweep covers at all: enabled and on disk"""
        return [p for p in self.catalog.projects
                if p.fleet_health.enabled and p.path.is_dir()]

    def stuck_tick(self, state: fleet.Fleet | None = None,
                   projects: list[ProjectSpec] | None = None) -> None:
        """Investigate every open order past its per-status threshold, capped and cooled"""
        projects = projects if projects is not None else self._stuck_projects()
        if not projects:
            return
        now = db.now()
        run: dict[str, Any] = {"ts": now, "scanned": 0, "candidates": 0, "opened": 0,
                               "skipped": {}, "excluded": "", "error": ""}
        errors: list[str] = []
        try:
            run["excluded"] = (self._stuck_exclusion(state)
                               or ("" if self.self_dispatch else "self_dispatch_off"))
            # COUNTED OVER EVERY ENABLED PROJECT, not over `projects`
            opened_today = sum(
                self.store_for(p).count_feature_orders(
                    "investigation", "fleet_health", now - 86400.0)
                for p in self._stuck_projects())
            candidates: list[dict[str, Any]] = []
            for project in projects:
                try:
                    candidates += self._stuck_candidates(project, state, now, run)
                except Exception as e:  # noqa: BLE001 — see `fleet.read`'s per-order rule
                    log.exception("the stuck sweep failed for %s", project.name)
                    errors.append(f"{project.name}: {type(e).__name__}: {e}")
            # MOST OVERDUE FIRST — the opposite sort to `_health_candidates`'
            candidates.sort(key=lambda c: c["verdict"].threshold_seconds
                            - c["verdict"].active_seconds)
            run["candidates"] = len(candidates)
            if not run["excluded"]:
                self._open_investigations(candidates, opened_today, now, run)
        except Exception as e:  # noqa: BLE001 — the row carries it; see the docstring
            log.exception("the stuck sweep failed")
            errors.append(f"{type(e).__name__}: {e}")
        run["error"] = "; ".join(errors)[:500]
        self.central.set_state(self.STUCK_RUN_KEY, db.to_json(run))

    def stuck_due_projects(self) -> list[ProjectSpec]:
        """Which projects this tick may sweep — each on its OWN `sweep_every_ticks`"""
        due = []
        for project in self._stuck_projects():
            last = self.stuck_swept.get(project.name)
            if last is not None \
                    and self.tick_count - last < project.fleet_health.sweep_every_ticks:
                continue
            self.stuck_swept[project.name] = self.tick_count
            due.append(project)
        return due

    @staticmethod
    def _stuck_exclusion(state: fleet.Fleet | None) -> str:
        """The fleet-wide hold that stops the sweep FILING, or `""`. §2, Neo 1074"""
        if state is None:
            return ""
        if state.shut():
            return "fleet_outage"
        if state.pause is not None:
            return "fleet_paused"
        if state.ramp is not None and state.at < state.ramp.until:
            return "fleet_ramp"
        return ""

    def _stuck_candidates(self, project: ProjectSpec, state: fleet.Fleet | None,
                          now: float, run: dict[str, Any]) -> list[dict[str, Any]]:
        """Every open order of one project that `stuck.assess` calls stuck"""
        from . import ops

        pstore = self.store_for(project)
        # PROJECT-WIDE, read once rather than once per candidate
        window = bool(pstore.health_sweep_hold()) or (state is not None and state.shut())
        # §1 and §2 of docs/superpowers/specs/2026-10-15-a-merge-held-by-a-red-base-is- not-stuck.md
        base_red, red_episodes, base_red_until = False, (), 0.0
        if project.validation.enabled and project.validation.auto_merge:
            # The freshness test is `_base_red`'s, REUSED and not re-derived
            base_red = self._base_red(project) is not None
            base_red_until = ops._base_red_until(self.central.base_health(project.name),
                                                 now)
            red_episodes = tuple(self.central.base_red_episodes(project.name))
        scanned, out = ops.stuck_scan(pstore, project.name, project.fleet_health, now,
                                      window=window, base_red=base_red,
                                      red_episodes=red_episodes,
                                      base_red_until=base_red_until)
        run["scanned"] += scanned
        return out

    def _open_investigations(self, candidates: list[dict[str, Any]], opened_today: int,
                             now: float, run: dict[str, Any]) -> None:
        """File one investigation per candidate, under the three rate limits of §6"""
        from . import ops, stuck

        cap = self.catalog.os.fleet_health.max_per_day
        for seen, candidate in enumerate(candidates):
            if opened_today + run["opened"] >= cap:
                run["skipped"]["daily_cap"] = len(candidates) - seen
                return
            wo, project = candidate["wo"], candidate["project"]
            verdict = candidate["verdict"]
            fingerprint = stuck.fingerprint(str(wo["status"]), candidate["since"],
                                            candidate["blocker"], candidate["events"])
            if ops.live_investigation(project, wo["id"]):
                run["skipped"]["live"] = run["skipped"].get("live", 0) + 1
                continue
            if self._stuck_cooled(project, wo["id"], fingerprint, now):
                run["skipped"]["cooldown"] = run["skipped"].get("cooldown", 0) + 1
                continue
            # A DIRECT PYTHON CALL, never a subprocess shelling out to `jarvis`: §2.7 of the investigation-orders...
            inv = ops.create_investigation_order(
                project, wo["id"],
                why=stuck.WHY.format(
                    subject=wo["id"], status_label=candidate["status_label"],
                    status_age=f"{(now - candidate['since']) / 3600:.1f}h",
                    activity_age=f"{(now - candidate['activity']) / 3600:.1f}h",
                    reason=verdict.reason, blocker=candidate["blocker"]),
                origin="fleet_health", fingerprint=fingerprint)
            run["opened"] += 1
            log.info("[%s] %s is stuck: %s — %s", project, wo["id"], verdict.reason,
                     inv["id"])

    def _stuck_cooled(self, project: str, wo_id: str, fingerprint: str,
                      now: float) -> bool:
        """Is this subject still inside its cooldown, or unchanged since last time? §6b"""
        from . import ops

        last = ops.last_stuck_investigation(project, wo_id)
        if last is None:
            return False
        cooldown = self.catalog.os.fleet_health.cooldown_minutes * 60.0
        return (now - float(last.get("updated_at") or 0.0) < cooldown
                or last[ops.STUCK_FINGERPRINT_KEY] == fingerprint)

    # -- 6b. the scheduler: work orders nobody typed

    def _os_owner(self) -> str | None:
        """Which project runs the fleet-wide OS checks — `schedule.os_owner`, per tick"""
        from . import schedule

        return schedule.os_owner((p.name, p.path) for p in self.catalog.projects)

    def _os_project(self) -> str | None:
        """Which project IS the OS — `schedule.os_project`, per tick"""
        from . import schedule

        return schedule.os_project((p.name, p.path) for p in self.catalog.projects)

    @staticmethod
    def _schedule_blocker(store: ProjectStore, state: dict[str, Any]) -> dict[str, Any] | None:
        """The job's own previous order, if it has not settled yet"""
        prev = state.get("last_wo_id")
        if not prev:
            return None
        try:
            wo = store.get_work_order(str(prev))
        except KeyError:
            return None
        return None if wo["status"] in TERMINAL_STATUSES else wo

    @staticmethod
    def _retire_previous(store: ProjectStore, state: dict[str, Any]) -> None:
        """Hide yesterday's receipt as today's is filed — the attention-budget rule"""
        prev = state.get("last_wo_id")
        if not prev:
            return
        try:
            wo = store.get_work_order(str(prev))
        except KeyError:
            return
        if (wo["status"] in TERMINAL_STATUSES and not wo["needs_attention"]
                and not wo["hidden"] and not store.pending_assumptions(wo["id"])):
            store.set_hidden(wo["id"])

    def schedule_tick(self, project: ProjectSpec, store: ProjectStore) -> None:
        """File the work orders this project's clock says are due (docs/superpowers/specs/2026-09-14-the-scheduler.md)"""
        from . import schedule

        cfg = project.schedule
        if not cfg.enabled or not cfg.jobs:
            return
        owner = self._os_owner()
        now = db.now()
        for job_id in cfg.jobs:
            try:
                spec = schedule.job(job_id)
            except KeyError:
                # `_parse_schedule` refuses an unknown id at boot, so this is only reachable on a DOWNGRADE
                log.warning("project %s: no scheduled job %r in this build",
                            project.name, job_id)
                continue
            state = store.seed_schedule(job_id, now)
            decision = schedule.decide(
                state, interval_seconds=cfg.interval_seconds, now=now,
                blocker=self._schedule_blocker(store, state))
            if decision.action == schedule.WAIT:
                continue
            if decision.action == schedule.HOLD:
                store.record_schedule_hold(job_id, decision.reason, now)
                continue
            ctx = schedule.JobContext(project=project.name,
                                      owns_os=owner == project.name)
            wo = store.create_work_order(title=spec.title, description=spec.describe(ctx),
                                         origin=schedule.ORIGIN)
            self._retire_previous(store, state)
            store.record_schedule_fire(job_id, wo["id"], now)
            log.info("scheduled job %s filed %s in %s", job_id, wo["id"], project.name)

    def abandon_unargued_gates(self, project: ProjectSpec, store: ProjectStore) -> None:
        """Close every gate request whose case never came"""
        from . import gates

        if not project.gates:
            return
        try:
            closed = gates.sweep_unargued(store, project.gates.case_ttl_seconds)
        except Exception:  # noqa: BLE001 — one project's sweep must not stop the tick
            log.exception("project %s: closing unargued gates failed", project.name)
            return
        for approval in closed:
            # "abandoned", never "refused": nobody reviewed it — spec 2026-09-12 §4
            log.info("gate %s (%s) abandoned: no case was made and the match was never "
                     "contested", approval["id"], approval["kind"])

    # -- 7. invariants (post-conditions)

    def check_invariants(self, project: ProjectSpec, store: ProjectStore, *,
                         sweep_landings: bool = False) -> None:
        """Verify the OS's own state and repair what is unambiguously wrong"""
        from .invariants import check_project

        try:
            violations = check_project(store, repair=True, slow=sweep_landings)
        except Exception:  # noqa: BLE001 — the checker must never take the daemon down
            log.exception("[%s] invariant check failed", project.name)
            return

        if sweep_landings:
            for invariant, wo_id in store.close_violation_reports(
                    v.key for v in violations):
                log.info("[%s] %s%s no longer violated", project.name,
                         f"{wo_id}: " if wo_id else "", invariant)

        for v in violations:
            # `level` and `detail` ride along so a standing CRITICAL one can reach the attention list from state...
            first = store.open_violation_report(v.invariant, v.key[1], level=v.level,
                                                detail=v.detail, owed=v.owed)
            # A DECISION IS OWED: raise the work order's own flag, with the reason DERIVED rather than written
            routed = bool(v.owed and v.wo_id)
            if routed:
                try:
                    wo = store.get_work_order(v.wo_id)
                except KeyError:
                    wo = None       # named an order deleted since the check ran
                if wo is not None and not wo["needs_attention"]:
                    blockers = invariants_mod.true_blockers(store, wo)
                    if blockers:
                        store.flag_attention(wo["id"], blockers[0])
            if not first:
                continue
            log.warning("[%s] %s", project.name, v)
            if v.wo_id:
                store.add_event(v.wo_id, "invariant", {
                    "invariant": v.invariant, "detail": v.detail,
                    "repaired": v.repaired, "repair": v.repair, **v.context,
                })
            if not v.repaired and not routed:
                # Nothing deterministic to do about it — this one needs a human
                store.add_notification(
                    title=f"OS invariant violated: {v.invariant}",
                    body=v.notice or (f"{v.detail}"
                                      + (f" ({v.wo_id})" if v.wo_id else "")),
                    # The violation's OWN level: most are `warning`
                    level=v.level, wo_id=v.wo_id, source="invariants",
                )

    def rules_tick(self, project: ProjectSpec, store: ProjectStore) -> dict[str, Any]:
        """Evaluate the rule registry against this project's open orders (docs/superpowers/specs/2026-09-27-self-evolution.md)"""
        counts: dict[str, Any] = {
            "enabled": False, "detectors": 0, "orders": 0, "opened": 0, "closed": 0,
            "unreadable": 0, "capped": "", "proposed": 0, "refused": 0,
            "recorded_armed": 0,
        }
        # OFF MEANS THE PASS DOES NOT RUN AT ALL — not "runs in dry run" (§5.2)
        if not project.rules.enabled:
            return counts
        counts["enabled"] = True

        try:
            return self._rules_tick(project, store, counts)
        except Exception:  # noqa: BLE001 — a heuristic must never take the daemon down
            log.exception("[%s] rules tick failed", project.name)
            return counts

    def _rules_tick(self, project: ProjectSpec, store: ProjectStore,
                    counts: dict[str, Any]) -> dict[str, Any]:
        from . import health, ops, rules

        now = db.now()
        deadline = time.monotonic() + EVAL_MAX_SECONDS

        # ONE QUERY FOR THE TICK
        detectors = self.central.list_detectors(project=project.name)
        readable, unreadable = rules.readable_detectors(detectors)
        counts["detectors"] = len(readable)

        # ONE `unreadable` ROW PER DETECTOR PER TICK, NOT PER ORDER
        for detector_id, problem in unreadable:
            counts["unreadable"] += 1
            self.central.record_rule_fire(
                detector_id=detector_id, project=project.name, order_id="",
                order_kind=rules.DEFAULT_SUBJECT, fingerprint="",
                mode=rules.DRY_RUN, outcome=rules.UNREADABLE,
                detail=f"the stored condition could not be read: {problem}")
        if not readable:
            return counts

        # THE LAZY SOURCE UNION (§5.2.3)
        sources: set[str] = set()
        for _row, cond in readable:
            sources |= set(rules.sources_used(cond))

        for wo in self._rules_candidates(project.name, store, counts):
            if time.monotonic() > deadline:
                # STOPS AND SAYS SO
                counts["capped"] = "seconds"
                log.info("[%s] rules tick hit its %.0fs budget after %d orders",
                         project.name, EVAL_MAX_SECONDS, counts["orders"])
                break
            counts["orders"] += 1
            # PER ORDER, NOT PER PASS: the break above is mid-pass
            self.rules_cursor[project.name] = (float(wo["created_at"] or 0.0),
                                               str(wo["id"]))
            try:
                # BUILT ONCE PER ORDER and handed to every detector: fifty rules cost one snapshot, not fifty (§5.2.2)
                facts = ops.rule_facts(store, wo, now=now, sources=frozenset(sources),
                                       project=project.name,
                                       max_rounds=project.validation.max_rounds)
                fingerprint = health.fingerprint(
                    store, {"kind": "work_order", "row": wo})
            except Exception as e:  # noqa: BLE001
                # PER ORDER, like `check_project`'s discipline for a broken invariant: one order whose snapshot...
                counts["unreadable"] += 1
                log.warning("[%s] no rule snapshot for %s: %s",
                            project.name, wo["id"], e)
                self.central.record_rule_fire(
                    detector_id="", project=project.name, order_id=str(wo["id"]),
                    order_kind=rules.DEFAULT_SUBJECT, fingerprint="",
                    mode=rules.DRY_RUN, outcome=rules.UNREADABLE,
                    detail=f"the fact snapshot could not be built: {e}")
                continue
            for row, cond in readable:
                self._evaluate_detector(project, row, cond, facts, fingerprint, counts,
                                        store, wo)
        return counts

    @staticmethod
    def _rules_rotate(rows: list[dict[str, Any]],
                      cursor: tuple[float, str] | None) -> list[dict[str, Any]]:
        """The same oldest-first list, rotated to resume just AFTER `cursor`"""
        if not rows or not cursor:
            return rows
        keys = [(float(r["created_at"] or 0.0), str(r["id"])) for r in rows]
        if cursor in keys:
            start = keys.index(cursor) + 1
        else:
            after = [i for i, key in enumerate(keys) if key > cursor]
            start = after[0] if after else 0
        return rows[start % len(rows):] + rows[:start % len(rows)]

    def _rules_candidates(self, project_name: str, store: ProjectStore,
                          counts: dict[str, Any]) -> list[dict[str, Any]]:
        """The open orders this tick will look at, OLDEST FIRST, rotated and capped (docs/superpowers/specs/2026-09-27-self-evolution.md)"""
        statuses = tuple(s for s in OPEN_STATUSES if s != "budget_exhausted")
        rows = store.list_work_orders(statuses=statuses, limit=EVAL_MAX_ORDERS * 5)
        rows.sort(key=lambda r: (float(r["created_at"] or 0.0), str(r["id"])))
        rows = self._rules_rotate(rows, self.rules_cursor.get(project_name))
        if len(rows) > EVAL_MAX_ORDERS:
            counts["capped"] = "orders"
            log.info("rules tick capped at %d of %d open orders",
                     EVAL_MAX_ORDERS, len(rows))
            rows = rows[:EVAL_MAX_ORDERS]
        return rows

    def _evaluate_detector(self, project: ProjectSpec, row: dict[str, Any],
                           cond: dict[str, Any], facts: Any, fingerprint: str,
                           counts: dict[str, Any], store: ProjectStore | None = None,
                           wo: dict[str, Any] | None = None) -> None:
        """One detector against one order's facts (docs/superpowers/specs/2026-09-27-self-evolution.md)"""
        from . import rules

        detector_id = str(row.get("id") or "")
        order_id = facts.order_id
        try:
            matched = rules.evaluate(cond, facts).matched
        except Exception as e:  # noqa: BLE001
            counts["unreadable"] += 1
            log.warning("[%s] detector %s raised on %s: %s",
                        project.name, detector_id, order_id, e)
            self.central.record_rule_fire(
                detector_id=detector_id, project=project.name, order_id=order_id,
                order_kind=facts.order_kind, fingerprint=fingerprint,
                mode=rules.DRY_RUN, outcome=rules.UNREADABLE,
                detail=f"the detector raised while being evaluated: {e}")
            return

        # THE DEDUPE MEMORY is the NEWEST open fire and never every past one
        open_fire = self.central.open_rule_fire(detector_id, order_id)
        if matched:
            if open_fire is not None and str(open_fire["fingerprint"]) == fingerprint:
                # A condition standing for six hours is ONE fire, not 720
                return
            # §6, wo-2877145f Neo 1649: an ARMED detector is handled by `_fire_armed`
            taken = None
            if store is not None and wo is not None:
                taken = self._fire_armed(project, row, cond, facts, fingerprint,
                                         counts, store, wo, open_fire)
            if taken == "held":
                return
            counts["opened"] += 1
            if taken:
                return
            self.central.record_rule_fire(
                detector_id=detector_id, project=project.name, order_id=order_id,
                order_kind=facts.order_kind, fingerprint=fingerprint,
                # `dry_run` unless `_fire_armed` took the fire above
                mode=rules.DRY_RUN, outcome=rules.RECORDED,
                remedy_rule_id="",
                detail=rules.render_condition(cond))
        elif open_fire is not None:
            counts["closed"] += 1
            self.central.close_rule_fire(int(open_fire["id"]))
            if open_fire["mode"] == rules.ARMED and store is not None:
                # An ARMED fire cleared: the order's timeline says so (§6)
                store.add_event(order_id, "rule_cleared", {
                    "alarm_id": open_fire["alarm_id"], "detector_id": detector_id,
                    "fire_id": int(open_fire["id"]),
                    "seconds": max(0.0, db.now() - float(open_fire["ts"]))})

    def _fire_armed(self, project: ProjectSpec, row: dict[str, Any],
                    cond: dict[str, Any], facts: Any, fingerprint: str,
                    counts: dict[str, Any], store: ProjectStore,
                    wo: dict[str, Any],
                    open_fire: dict[str, Any] | None) -> str | None:
        """THE DETECTOR'S STATUS BUYS VISIBILITY, THE PAIR GATES THE PROPOSE (docs/superpowers/specs/2026-09-27-self-evolution.md)"""
        from . import remedies, rules
        from .neo_store import NeoStore
        from .project_store import NO_TURN

        detector_id = str(row.get("id") or "")
        order_id = facts.order_id
        if row.get("status") != rules.ARMED:
            return None
        remedy_row: dict[str, Any] | None = None
        alarm: dict[str, Any] | None = None
        try:
            remedy_row = next((r for r in self.central.remedy_rules_for(detector_id)
                               if rules.effective_status(row, r) == rules.ARMED), None)
            awaiting = ("proposed",) if remedy_row else ("informational",)
            if (open_fire is not None and open_fire["mode"] == rules.ARMED
                    and open_fire["alarm_id"]
                    and store.get_alarm(open_fire["alarm_id"])["status"] in awaiting):
                return "held"
            reason = rules.render_condition(cond)
            if remedy_row is None:
                # informational: nothing can be proposed
                alarm = store.add_finding(
                    order_id, kind=str(row["gap_class"]), reason=reason, seq=NO_TURN,
                    source="rule", probe=detector_id, status="informational")
                store.add_event(order_id, "rule_fired", {
                    "alarm_id": alarm["id"], "detector_id": detector_id, "reason": reason})
                # Nothing filed a gate request, so the pre-action fingerprint stands (the propose path below...
                self.central.record_rule_fire(
                    detector_id=detector_id, project=project.name, order_id=order_id,
                    order_kind=facts.order_kind, fingerprint=fingerprint,
                    mode=rules.ARMED, outcome=rules.RECORDED, remedy_rule_id="",
                    alarm_id=str(alarm["id"]),
                    detail="the detector is armed with no armed remedy, so nothing was "
                           f"proposed: {reason}")
                counts["recorded_armed"] += 1
                return "fired"
            primitive, argument = str(remedy_row["primitive"]), str(
                remedy_row["argument"] or "")
            evidence = rules.bound(rules.explain(cond, rules.evaluate(cond, facts)))
            alarm = store.add_finding(
                order_id, kind=str(row["gap_class"]), reason=reason, seq=NO_TURN,
                source="rule", probe=detector_id, remedy=primitive,
                remedy_argument=argument)
            store.add_event(order_id, "rule_fired", {
                "alarm_id": alarm["id"], "detector_id": detector_id,
                "remedy_rule_id": remedy_row["id"], "remedy": primitive,
                "reason": reason})
            neo = NeoStore()   # thread-local connection, as every other opener here
            try:
                outcome = remedies.propose(
                    store, neo, project.name, dict(wo), alarm, primitive, argument,
                    project.supervisor.remedies, evidence=evidence, reason=reason)
            finally:
                neo.close()
        except Exception as e:  # noqa: BLE001 — recorded, never defaulted
            counts["unreadable"] += 1
            words = f"{type(e).__name__}: {e}"
            log.warning("[%s] armed detector %s could not propose on %s: %s",
                        project.name, detector_id, order_id, words)
            if alarm is not None:
                try:
                    store.update_alarm(alarm["id"], status="failed",
                                       verdict_reason=words)
                except Exception:  # noqa: BLE001
                    log.exception("could not mark alarm %s failed", alarm["id"])
            fire = self.central.record_rule_fire(
                detector_id=detector_id, project=project.name, order_id=order_id,
                order_kind=facts.order_kind, fingerprint=fingerprint,
                mode=rules.ARMED, outcome=rules.UNREADABLE,
                remedy_rule_id=str(remedy_row["id"]) if remedy_row else "",
                alarm_id=str(alarm["id"]) if alarm else "",
                detail=f"the armed fire could not be proposed: {words}")
            self.central.close_rule_fire(int(fire["id"]))
            return "fired"

        proposed = bool(outcome.get("proposed"))
        counts["proposed" if proposed else "refused"] += 1
        # THE FINGERPRINT IS TAKEN AFTER THE ACTION
        try:
            from . import health
            fingerprint = health.fingerprint(
                store, {"kind": "work_order", "row": store.get_work_order(order_id)})
        except Exception:  # noqa: BLE001 — keep the pre-action string rather than lose the fire
            log.exception("[%s] no post-action fingerprint for %s", project.name, order_id)
        self.central.record_rule_fire(
            detector_id=detector_id, project=project.name, order_id=order_id,
            order_kind=facts.order_kind, fingerprint=fingerprint, mode=rules.ARMED,
            outcome=rules.PROPOSED if proposed else rules.REFUSED,
            remedy_rule_id=str(remedy_row["id"]), alarm_id=str(alarm["id"]),
            detail=reason if proposed else str(outcome.get("reason") or ""))
        return "fired"

    # -- 2 & 6. turns, settlement, and injected sessions

    def check_burning_turns(self, project: ProjectSpec, store: ProjectStore) -> None:
        """Raise a turn that is costing money WHILE it is still costing it"""
        from . import usage as usage_mod

        # The PROJECT's thresholds, already resolved against the OS block by `catalog._parse_inspect`
        cfg = project.inspect
        if not cfg.enabled:
            return
        now = time.time()
        index = usage_mod.index_sessions()
        for wo in store.list_work_orders(statuses=("running",)):
            session_id = wo.get("session_id")
            if not session_id or wo["origin"] in UNGOVERNED_ORIGINS:
                continue
            turn = store.latest_turn(wo["id"])
            if turn is None or turn["state"] != "running":
                continue
            try:
                raised = inspection.live_alarms(
                    session_id, cfg, wo_id=wo["id"], now=now, index=index,
                    dispatched=turn["started_at"],
                    # What the OS was itself holding this order for
                    spans=holds.held(store, wo["id"], now=now),
                    # Spec 2026-09-27 §3: the alarm and `jarvis inspect` name one turn the same way
                    turn_starts=store.turn_starts(wo["id"]))
            except OSError:
                continue  # a transcript Jarvis cannot read is not a work order in trouble
            seen = [db.from_json(e["payload"], {}) or {}
                    for e in store.events_of_kind(wo["id"], "cost_alarm")]
            already = {p.get("kind") for p in seen if p.get("seq") == turn["seq"]}
            fresh = [a for a in raised if a.kind not in already]
            for alarm in fresh:
                # THE ROW IS THE IDENTITY; THE EVENT IS STILL THE DEDUPE MEMORY
                row = store.add_alarm(
                    wo["id"], alarm.kind, turn["seq"], alarm.reason,
                    status=("informational"
                            if alarm.kind in inspection.INFORMATIONAL_KINDS
                            else "raised"))
                store.add_event(wo["id"], "cost_alarm",
                                {"kind": alarm.kind, "seq": turn["seq"],
                                 "reason": alarm.reason, "alarm_id": row["id"]})
                log.info("[%s] %s: %s", project.name, wo["id"], alarm.reason)
            # Every alarm goes on the timeline
            interrupting = [a for a in fresh
                            if a.kind not in inspection.INFORMATIONAL_KINDS]
            if interrupting and not wo["needs_attention"]:
                store.flag_attention(wo["id"], interrupting[0].reason)

    def check_rewrite_tax(self, project: ProjectSpec, store: ProjectStore) -> None:
        """Raise a project's STANDING re-write tax, split by the cause that produced it"""
        from . import bill as bill_mod
        from .project_store import NO_TURN

        cfg = project.inspect
        if not cfg.enabled:
            return
        tax = bill_mod.rewrite_tax(store, days=cfg.alarm_rewrite_window_days)
        if tax is None:
            return
        # BOTH floors, and they answer different questions: too few orders is one order's shape wearing the...
        if tax.orders < cfg.alarm_rewrite_min_orders \
                or tax.bill_usd < cfg.alarm_rewrite_min_usd:
            return
        # `(kind, reason)` and not an `inspection.Alarm`: `bill` is accounting and does not import the config...
        for kind, reason in bill_mod.rewrite_alarms(tax, cfg):
            last = store.last_alarm_of_kind(kind)
            if last is not None and float(last["ts"] or 0.0) >= tax.since:
                continue
            row = store.add_finding(tax.worst_id, kind=kind, reason=reason,
                                    seq=NO_TURN, source="cost")
            store.add_event(tax.worst_id, "cost_alarm",
                            {"kind": kind, "seq": NO_TURN,
                             "reason": reason, "alarm_id": row["id"]})
            self.central.add_inbox(
                project=project.name, level="warning",
                title=REWRITE_INBOX_TITLE[kind].format(project=project.name),
                body=f"{reason}\n"
                     f"The supervisor will look before you have to. "
                     f"Read it with: jarvis alarms show {row['id']}",
                wo_id=tax.worst_id)
            log.info("[%s] %s: %s", project.name, kind, reason)

    def _turn_arms(self, since: float) -> dict[str, list[tuple[float, float, str]]]:
        """Every project's stamped turns, indexed by session id (docs/superpowers/specs/2026-10-15-the-hour-on-worker-turns-only.md)"""
        index: dict[str, list[tuple[float, float, str]]] = {}
        for project in self.catalog.projects:
            if not project.path.is_dir():
                continue
            try:
                rows = self.store_for(project).turn_arms(since)
            except Exception:
                log.exception("[%s] cache-TTL arms unreadable", project.name)
                continue
            for session_id, started_at, ended_at, arm in rows:
                index.setdefault(session_id, []).append((started_at, ended_at, arm))
        return index

    def _arm_rate_rows(self, since: float) -> list[tuple[str, str, int, str, float,
                                                          int, int]]:
        """Every project's stamped turns with the TTL split they actually paid (issue 1227)"""
        rows: list[tuple[str, str, int, str, float, int, int]] = []
        for project in self.catalog.projects:
            if not project.path.is_dir():
                continue
            try:
                found = self.store_for(project).turn_arm_rates(since)
            except Exception:
                log.exception("[%s] cache-TTL rates unreadable", project.name)
                continue
            for wo_id, seq, arm, started_at, cache_1h, cache_5m in found:
                rows.append((project.name, wo_id, seq, arm, started_at,
                             cache_1h, cache_5m))
        return rows

    def check_cache_ttl(self, project: ProjectSpec, store: ProjectStore) -> None:
        """Raise the fleet's remaining ONE-HOUR cache writes, split by who bought them (docs/superpowers/findings/2026-08-30-where-the-800-dollars-went.md)"""
        from .project_store import NO_TURN

        cfg = project.inspect
        if not cfg.enabled or project.name != self._os_owner():
            return
        days = cfg.alarm_cache_1h_window_days
        since = db.now() - days * 86_400
        found: list[inspection.HourWrites] = []
        try:
            found = inspection.one_hour_writes(since, arms=self._turn_arms(since))
        except OSError:  # a transcript tree that moved or is unreadable
            log.exception("[%s] the one-hour cache scan could not read transcripts",
                          project.name)
        raised = inspection.hour_alarms(found, cfg, days=days)
        # Reads the RECORD, not the transcripts, so it holds when they do not read
        raised += inspection.arm_miss_alarms(
            inspection.arm_misses(self._arm_rate_rows(since)), cfg, days=days)
        if not raised:
            return
        carrier = store.latest_settled_order()
        if carrier is None:
            # Nothing to hang the foreign key on
            log.info("[%s] one-hour cache writes found, but no settled order to carry "
                     "the alarm yet", project.name)
            return
        for alarm in raised:
            last = store.last_alarm_of_kind(alarm.kind)
            if last is not None and float(last["ts"] or 0.0) >= since:
                continue
            row = store.add_finding(carrier["id"], kind=alarm.kind, reason=alarm.reason,
                                    seq=NO_TURN, source="cost")
            store.add_event(carrier["id"], "cost_alarm",
                            {"kind": alarm.kind, "seq": NO_TURN,
                             "reason": alarm.reason, "alarm_id": row["id"]})
            self.central.add_inbox(
                project=project.name, level="warning",
                title=CACHE_1H_INBOX_TITLE[alarm.kind],
                body=f"{alarm.reason}\n"
                     f"The supervisor will look before you have to. "
                     f"Read it with: jarvis alarms show {row['id']}",
                wo_id=carrier["id"])
            log.info("[%s] %s: %s", project.name, alarm.kind, alarm.reason)

    def check_meter_residual(self, project: ProjectSpec, store: ProjectStore) -> None:
        """Reconcile the account's meter against measured spend, and raise the two gaps"""
        from . import fleetcost
        from .project_store import NO_TURN

        if project.name != self._os_owner():
            return
        cfg = project.cost
        try:
            resolved = fleetcost.resolve_window(window=fleetcost.SESSION, cfg=cfg)
            meter = usage_meter.reconciliation(resolved=resolved,
                                               project=None, cfg=cfg)["meter"]
        except Exception:  # noqa: BLE001 — a failing pass must not take the tick down
            log.exception("[%s] the usage meter could not be reconciled", project.name)
            return
        if meter.get("unreliable"):
            # A suppressed pass is visible in the log and nowhere else: the user has nothing to do about a defect...
            log.warning("[%s] the usage meter does not reconcile over this span (%s): "
                        "no residual alarm is raised from it", project.name,
                        ", ".join(meter["unreliable"]))
        raised = [kind for kind, flag in (
            (METER_OUTSIDE_ALARM, meter["alerts"]["outside"]),
            (METER_RESIDUAL_ALARM, meter["alerts"]["residual"])) if flag]
        if not raised:
            return
        carrier = store.latest_settled_order()
        if carrier is None:
            # Nothing to hang the foreign key on
            log.info("[%s] the meter does not reconcile, but no settled order can carry "
                     "the alarm yet", project.name)
            return
        for kind in raised:
            last = store.last_alarm_of_kind(kind)
            if last is not None and float(last["ts"] or 0.0) >= resolved["since"]:
                continue
            reason = meter_alarm_reason(kind, meter)
            row = store.add_finding(carrier["id"], kind=kind, reason=reason,
                                    seq=NO_TURN, source="cost")
            store.add_event(carrier["id"], "cost_alarm",
                            {"kind": kind, "seq": NO_TURN, "reason": reason,
                             "alarm_id": row["id"]})
            self.central.add_inbox(
                project=project.name, level="warning", title=METER_INBOX_TITLE[kind],
                body=f"{reason}\n"
                     f"The supervisor will look before you have to. "
                     f"Read it with: jarvis alarms show {row['id']}",
                wo_id=carrier["id"])
            log.info("[%s] %s: %s", project.name, kind, reason)

    def settle_turns(self, project: ProjectSpec, store: ProjectStore) -> None:
        """Reap finished turns, then move each work order to where its turn says it is"""
        for turn in worker_session.poll(store, project.name):
            log.info("[%s] turn %s of %s ended: %s", project.name, turn["seq"],
                     turn["wo_id"], turn["state"])
        # `idle` is in the sweep, and it is what MIGRATES the managers this release finds already parked in... (#264)
        for wo in store.list_work_orders(statuses=SETTLE_STATUSES):
            if wo["origin"] in UNGOVERNED_ORIGINS:
                continue  # not ours to run; track_injected_sessions follows these
            try:
                self.settle_work_order(project, store, wo)
            except Exception:  # noqa: BLE001 — one work order must not stall the rest
                log.exception("[%s] settling %s failed", project.name, wo["id"])
        # A DEFERRED LANDING IS OWED IN `validating` AND NOWHERE ELSE (docs/superpowers/specs/2026-09-25-a-cap-hold-must-say-so.md)
        for wo in store.list_work_orders(statuses=("validating",)):
            if wo["origin"] in UNGOVERNED_ORIGINS:
                continue
            if _landing_deferred(store, wo["id"]) is None:
                continue
            try:
                self.settle_work_order(project, store, wo)
            except Exception:  # noqa: BLE001 — same isolation as the sweep above
                log.exception("[%s] settling %s failed", project.name, wo["id"])

    def settle_work_order(self, project: ProjectSpec, store: ProjectStore,
                          wo: dict) -> None:
        # `user_facing_wait` is NOT read here any more: it decides eligibility
        from .invariants import something_is_out, true_blockers

        # OUT OF MONEY, asked before the turn is looked at but AFTER the round machine's claim below
        latest_round = store.latest_validation_round(wo_id=wo["id"]) or {}
        round_open = latest_round.get("outcome")
        if round_open not in RUNNABLE_VALIDATION_OUTCOMES:
            spent = budget_mod.exhaustion(store, self.central, wo)
            if spent is not None:
                budget_mod.escalate(store, wo, spent)
                return
        if wo["status"] == budget_mod.EXHAUSTED:
            # Parked here and no longer over its cap: the user raised the budget
            return

        if wo["status"] == "validating":
            # THE ROUND MACHINE OWNS THIS WORK ORDER (docs/superpowers/specs/2026-09-25-a-cap-hold-must-say-so.md)
            from . import ops as ops_mod

            deferred = (_landing_deferred(store, wo["id"])
                        if round_open == "passed" else None)
            if (deferred is not None
                    and worker_session.busy(store, wo["id"]) is None
                    and not store.queued_messages(wo["id"])):
                fresh = store.get_work_order(wo["id"])
                ops_mod.land_when_cleared(store, fresh)
                store.add_event(wo["id"], VALIDATION_LANDED, {"round_id": deferred})
            return

        turn = store.latest_turn(wo["id"])
        if turn is None:
            if settle_grace_holds(wo):
                return  # just claimed; give the launch a moment to record its turn
            if wo.get("session_id") or wo.get("job_id"):
                # In flight when this release landed: dispatched under the background -session transport
                if not wo["needs_attention"]:
                    store.flag_attention(
                        wo["id"],
                        "carried over from the old worker transport — send it a "
                        f"message to resume it, or `jarvis wo cancel {wo['id']}`",
                    )
                    store.add_event(wo["id"], "pre_turn_carryover",
                                    {"job_id": wo.get("job_id"),
                                     "session_id": wo.get("session_id")})
                return
            # Claimed but never launched — the daemon died between the two writes
            store.set_status(wo["id"], "failed")
            store.flag_attention(wo["id"], "worker turn never started")
            return

        if turn["state"] == "running":
            if wo["status"] == "dispatching":
                store.set_status(wo["id"], "running")
            elif worker_session.is_stalled(turn) and not wo["needs_attention"]:
                hours = int((time.time() - turn["started_at"]) // 3600)
                store.flag_attention(
                    wo["id"],
                    f"turn running for over {hours}h — check on it or "
                    f"`jarvis wo cancel {wo['id']}`",
                )
            return

        if turn["state"] == "failed":
            # Lost to the transport, not broken: leave the work order exactly where it is and let...
            pause = worker_session.turn_pause(store, wo["id"])
            if pause and pause.reason == worker_session.PAUSE_AUTH:
                self._park_on_signin(store, wo, pause, turn)
                return
            if pause and not pause.exhausted:
                return
            if verdicts.verdict_landed(store, wo):
                # ASKED OF THE WORK, NOT THE PROCESS: a turn that died after its verdict is no failure
                self._settle_landed_investigator(store, wo, why="turn_ended_after_verdict")
                return
            if wo["status"] != "failed":
                # THE ORDER IS `failed` EITHER WAY
                delivered = invariants_mod.has_delivered(store, wo)
                store.set_status(wo["id"], "failed")
                if delivered:
                    store.add_event(
                        wo["id"], invariants_mod.TURN_DIED_AFTER_DELIVERY_EVENT,
                        {"error": turn.get("error"),
                         **({"attempts": pause.attempts, "reason": pause.reason,
                             "message": pause.message} if pause else {})})
                    # ONLY IF THERE IS A BLOCKER TO FLAG
                    fresh_row = store.get_work_order(wo["id"])
                    blockers = invariants_mod.true_blockers(store, fresh_row)
                    if blockers:
                        store.flag_attention(wo["id"], blockers[0])
                else:
                    store.flag_attention(wo["id"],
                                         invariants_mod.failed_blocker(store, wo))
                if pause:
                    # Retried until the OS ran out of patience
                    store.add_event(wo["id"], "turn_retries_exhausted",
                                    {"attempts": pause.attempts,
                                     "reason": pause.reason,
                                     "error": pause.message})
                if delivered:
                    # THE PHONE HAS TO SAY WHICH FAILURE THIS IS
                    title = f"{wo['id']} delivered, then its session died"
                    said = ["The work was delivered and is on record; the order is "
                            "`failed` because the turn never finished."]
                    if wo.get("pr_url"):
                        said.append(f"Pull request: {wo['pr_url']}.")
                    n = len(store.all_assumptions(wo["id"]))
                    if n:
                        said.append(f"{n} assumption{'s' if n != 1 else ''} on record.")
                    said.append(f"`jarvis wo retry {wo['id']}` resumes the session where "
                                f"it died; `jarvis wo done {wo['id']}` closes it.")
                    said.append(turn.get("error") or "no error recorded")
                    body = " ".join(said)
                else:
                    title = (f"{wo['id']} still failing after {pause.attempts} "
                             f"{worker_session.PAUSE_NOUN[pause.reason]} retries" if pause
                             else f"{wo['id']} worker turn failed")
                    body = (turn.get("error") or "no error recorded")[:500]
                store.add_notification(title=title, body=body[:500], level="warning",
                                       wo_id=wo["id"], source="reconciler", paired=True)
            return

        # The turn is done
        fresh = store.get_work_order(wo["id"])
        if not settles_done_turn(store, fresh):
            return
        from . import ops as ops_mod

        # THE LANDING THE ROUND MACHINE DEFERRED (docs/superpowers/specs/2026-09-25-a-cap-hold-must-say-so.md)
        deferred = _landing_deferred(store, wo["id"]) if round_open == "passed" else None
        if deferred is not None:
            ops_mod.land_when_cleared(store, fresh)
            store.add_event(wo["id"], VALIDATION_LANDED, {"round_id": deferred})
            return
        if fresh.get("result_summary"):
            if invariants_mod.reviewable_assumptions(store, fresh):
                if fresh["status"] != "needs_review":
                    store.set_status(wo["id"], "needs_review")
                    store.flag_attention(wo["id"], "assumptions pending review")
            elif ops_mod.routes_on_pull_request(store, fresh):
                # Finished behind a pull request: it is the user's merge that ends this work order (#224)

                # `resumed_from` is the same rule as `pr_repair_origin` for the other way the OS takes a work order... (#259)
                back = ("needs_review"
                        if invariants_mod.validation_escalated(store, fresh)
                        else ops_mod.pr_repair_origin(store, wo["id"])
                        or ops_mod.resumed_from(store, wo["id"], turn["seq"])
                        or "waiting_pr_merge")
                if fresh["status"] != back:
                    # Spec §C of docs/superpowers/specs/2026-10-14-a-message-must-not-drop-an-owed-review.md
                    store.set_status(wo["id"], back, trigger="turn_settled")
                    if back == "waiting_pr_merge":
                        store.clear_attention(wo["id"])
            else:
                # THROUGH `land_finished`, never straight to `completed`
                ops_mod.land_finished(store, fresh)
        elif wo.get("kind") != "manager" and something_is_out(store, wo["id"]):
            # Parked on the delegate — a privileged-action gate awaiting a verdict (docs/superpowers/specs/2026-09-29-a-heredoc-edit-is-not-a-merge.md)
            store.set_status(wo["id"], "waiting_input")
        elif wo.get("kind") == "manager":
            # A project manager order is idle BY DESIGN: it acts on a message and ends its turn (#264)
            parent = wo.get("parent_id")
            feature = store.get_feature_order(parent) if parent else None
            if feature and feature["status"] in FO_TERMINAL_STATUSES:
                self._close_feature_manager(store, str(parent))
            else:
                store.set_status(wo["id"], "idle")
                # The one write the re-status cannot do on its own: a manager carried over from before issue #264 may...
                moved = store.get_work_order(wo["id"])
                if moved["needs_attention"] and not true_blockers(store, moved):
                    store.clear_attention(wo["id"])
        else:
            from .invariants import IDLE_NO_FINISH_BLOCKER

            # THE OS SENDS IT BACK ITSELF WHEN IT KNOWS WHY IT STOPPED
            if background.nudge(store, fresh):
                return

            store.set_status(wo["id"], "needs_review")
            store.flag_attention(wo["id"], IDLE_NO_FINISH_BLOCKER)

    @staticmethod
    def _settle_landed_investigator(store: ProjectStore, wo: dict[str, Any], *,
                                    why: str) -> None:
        """`completed`, flag down, no notification: what `submit_verdict` writes (io-d2a5dd69)"""
        from . import ops as ops_mod

        if wo["status"] in ("completed", "cancelled"):
            # Settled already; `failed` is not, since a stored verdict means it did not fail
            store.clear_attention(wo["id"])
            return
        ops_mod.settle_in_session(store, wo, "verdict_settled_by_reconciler",
                                  payload={"why": why})

    def settle_landed_investigators(self, project: ProjectSpec,
                                    store: ProjectStore) -> None:
        """Retire the status or flag a landed investigator still holds (io-d2a5dd69)"""
        for fo in store.list_feature_orders(statuses=("completed",),
                                            kind="investigation"):
            if not fo.get("plan_wo_id"):
                continue
            try:
                wo = store.get_work_order(fo["plan_wo_id"])
            except KeyError:
                continue
            if wo["status"] == "completed" and not wo["needs_attention"]:
                continue
            if wo["status"] == "cancelled" and not wo["needs_attention"]:
                continue
            if not verdicts.verdict_landed(store, wo):
                continue
            self._settle_landed_investigator(store, wo, why="verdict_already_landed")
            log.info("[%s] %s: investigator of %s settled on its stored verdict (was %s)",
                     project.name, wo["id"], fo["id"], wo["status"])

    def _park_on_signin(self, store: ProjectStore, wo: dict[str, Any],
                        pause: worker_session.TurnPause,
                        turn: dict[str, Any]) -> None:
        """Hold a work order whose turn died on authentication, until the user signs in"""
        from .invariants import AUTH_BLOCKER

        if wo["status"] == "waiting_input":
            return
        store.set_status(wo["id"], "waiting_input")
        store.flag_attention(wo["id"], AUTH_BLOCKER)
        store.add_notification(
            # The title names the failure rather than the fact that there was one: this is the line the user...
            title=f"{wo['id']} parked — Claude Code could not authenticate",
            body=(turn.get("error") or pause.message)[:500],
            level="warning", wo_id=wo["id"], source="reconciler", paired=True,
        )

    # -- 6. pull requests parked on a human

    def poll_default_branch(self, project: ProjectSpec, store: ProjectStore) -> None:
        """Is this project's default branch red? (docs/superpowers/specs/2026-09-26-a-red-default-branch-raises-itself.md §1.)"""
        from . import ci, github
        from .invariants import INV_BASE_RED

        origin = github.origin_repo(project.path)
        if origin is None:
            return
        owner, repo = origin
        prev = self.central.base_health(project.name)
        cached = str(prev.get("base") or "")
        base_read_at = prev.get("base_read_at")
        try:
            # Cached in the same fact and re-read only when absent: the steady-state cost stays the one `gh run...
            base = cached or ci.default_branch(owner, repo, cwd=project.path)
            if not cached:
                base_read_at = time.time()
            runs = ci.base_runs(base, cwd=project.path)
            if not runs and cached:
                # A CACHED NAME IS THE ONE PART OF THIS FACT THAT GOES STALE SILENTLY: `gh run list --branch main` on...
                fresh = ci.default_branch(owner, repo, cwd=project.path)
                base_read_at = time.time()
                if fresh != base:
                    log.info("[%s] default branch is %s, not %s", project.name, fresh,
                             base)
                    base, runs = fresh, ci.base_runs(fresh, cwd=project.path)
        except github.GitHubError as e:
            log.debug("[%s] could not read the default branch's CI: %s", project.name, e)
            return
        workflows = tuple({r.workflow for r in runs if r.workflow})
        reds = [r for w in workflows if (r := ci.latest(runs, w)) is not None and r.red]
        reds.sort(key=lambda r: (r.started_at is not None, r.started_at or 0),
                  reverse=True)
        worst = reds[0] if reds else None
        was_red = bool(prev.get("red"))
        wo_id = issue_url = fix_wo_id = ""
        issue_os_filed = False
        if worst is not None:
            wo_id = (str(prev.get("wo_id") or "") if was_red
                     else self._attribute_base_red(project, store, owner, repo,
                                                   worst.head_sha))
            if was_red:
                # Spec 2026-10-14-a-red-main-dispatches-its-own-fix.md §3: carried forward exactly as `wo_id` is
                issue_url = str(prev.get("issue_url") or "")
                fix_wo_id = str(prev.get("fix_wo_id") or "")
                issue_os_filed = bool(prev.get("issue_os_filed"))
        fact = {
            "red": worst is not None,
            "base": base,
            "base_read_at": base_read_at or time.time(),
            "workflow": worst.workflow if worst else "",
            "run_id": worst.run_id if worst else 0,
            "run_url": ci.run_url(owner, repo, worst.run_id) if worst else "",
            "head_sha": worst.head_sha if worst else "",
            "wo_id": wo_id,
            "issue_url": issue_url,
            "fix_wo_id": fix_wo_id,
            "issue_os_filed": issue_os_filed,
            "ci_reruns": self._rerun_aborted_base_ci(project, store, owner, repo, base,
                                                     runs, prev.get("ci_reruns") or {}),
            "checked_at": db.now(),
        }
        self.central.set_base_health(project.name, fact)
        if was_red and worst is None:
            self._record_red_episode(project, red=False, now=fact["checked_at"])
            store.close_violation_report(INV_BASE_RED)
            store.add_notification(
                title=f"`{base}` is green again in {project.name}",
                body=(f"the default branch's build recovered — workflow "
                      f"`{prev.get('workflow') or 'ci'}` no longer fails, and the merges "
                      f"the OS was holding can proceed"),
                level="info", source="base-health",
            )
            log.info("[%s] %s recovered", project.name, base)
        elif worst is not None and not was_red:
            self._record_red_episode(project, red=True, now=fact["checked_at"])
            log.warning("[%s] %s is red — %s", project.name, base, fact["run_url"])

    def _record_red_episode(self, project: ProjectSpec, red: bool, now: float) -> None:
        """Open or close this project's red episode, on a TRANSITION only (docs/superpowers/specs/2026-10-15-a-merge-held-by-a-red-base-is-not-stuck.md)"""
        from .central_store import advance_red_episodes

        self.central.set_base_red_episodes(
            project.name,
            advance_red_episodes(
                self.central.base_red_episodes(project.name), red, now,
                project.fleet_health.red_episode_retention_hours * 3600.0))

    def _rerun_aborted_base_ci(self, project: ProjectSpec, store: ProjectStore,
                               owner: str, repo: str, base: str,
                               runs: tuple[Any, ...], prev: Any) -> dict[str, str]:
        """Re-run a default-branch run that JUDGED NOTHING, once each, so the base stays readable (#1193)"""
        from . import ci, ci_rerun, github

        state = {str(k): str(v) for k, v in prev.items()} if isinstance(prev, dict) else {}
        # The newest run of a workflow, NOT its newest completed one: an in-flight re-run must not read as a...
        newest: dict[str, Any] = {}
        for r in runs:
            if r.workflow:
                newest.setdefault(r.workflow, r)
        for workflow, run in newest.items():
            if (not run.completed or run.run_id <= 0
                    or run.conclusion not in ci_rerun.ABORTED_RUN_CONCLUSIONS):
                continue
            key = str(run.run_id)
            if state.get(key) == "stuck":
                continue
            if state.get(key) == "rerun":
                state[key] = "stuck"
                store.add_notification(
                    title=f"`{base}` has no CI verdict in {project.name}",
                    body=(f"workflow `{workflow}` on {run.head_sha[:10] or 'its head'} "
                          f"ended `{run.conclusion}` again after the OS re-ran it once, "
                          f"so {ci.run_url(owner, repo, run.run_id)} says nothing about "
                          f"the code — run `{ci_rerun.rerun_command(run.run_id)}`"),
                    level="warning", source="base-health",
                )
                log.warning("[%s] %s CI on %s was %s again after one re-run (run %s)",
                            project.name, base, workflow, run.conclusion, run.run_id)
                continue
            try:
                ci_rerun.rerun(run.run_id, cwd=project.path)
            except github.GitHubError as e:
                # Unread is not unrecoverable: no attempt is spent, the next tick retries
                log.info("[%s] could not re-run %s on %s: %s", project.name, run.run_id,
                         base, e)
                continue
            state[key] = "rerun"
            log.info("[%s] re-ran %s on %s — it ended %s and judged nothing",
                     project.name, workflow, base, run.conclusion)
        # Remembered only while the run still has NO verdict: an in-flight re-run counts as none
        live = {str(r.run_id) for r in runs
                if not r.completed or r.conclusion in ci_rerun.ABORTED_RUN_CONCLUSIONS}
        return {k: v for k, v in state.items() if k in live}

    def dispatch_base_fix(self, project: ProjectSpec, store: ProjectStore) -> None:
        """A red tracker `main` files-or-adopts an issue and dispatches a work order on it (docs/superpowers/specs/2026-10-14-a-red-main-dispatches-its-own-fix.md §2.)"""
        from . import bugreport, github, issues

        origin = github.origin_repo(project.path)
        repo = bugreport.bug_repo()
        if origin is None or f"{origin[0]}/{origin[1]}".lower() != repo.lower():
            return
        if self._base_red(project) is None:
            return
        fact = self.central.base_health(project.name)
        run_id = fact.get("run_id") or 0
        issue_url = str(fact.get("issue_url") or "")
        fix_wo_id = str(fact.get("fix_wo_id") or "")
        # §3: True only for an issue that passed §4's author-plus-marker test
        issue_os_filed = bool(fact.get("issue_os_filed"))
        if fix_wo_id:
            # §2 step 4: one fix work order per break, EVER, whatever became of it
            return
        if not self._base_red_outlived_grace(project):
            return
        # The steward's call... (docs/superpowers/specs/2026-10-21-a-steward-decides-what-the-os-may-spend-on-itself.md §3.2)
        decision = steward.gate(
            steward.Candidate("red_main", "critical", project.name,
                              f"`{fact.get('base') or 'main'}` is red",
                              evidence=(str(fact.get("run_url") or run_id or ""),),
                              parent_wo_id=str(fact.get("wo_id") or "")),
            catalog=self.catalog, store=store,
            self_dispatch=self.self_dispatch)
        if not self.self_dispatch:
            return  # the master switch (A12): decided and recorded, nothing filed
        cleared = False
        try:
            # §2 step 5: READ THE STATE rather than parse the refusal
            if issue_url and issues.view(issue_url, repo).closed:
                log.info("[%s] %s is closed and no work order came of it — the break is "
                         "filed afresh on the next tick", project.name, issue_url)
                # The url ONLY: `fix_wo_id` is necessarily empty here, per step 4 above
                issue_url = ""
                issue_os_filed = False
                cleared = True
            elif not issue_url:
                issue_url = self._adopt_base_red_issue(repo, run_id)
                # Adoption IS §4's test, so an adopted url is the OS's own filing
                issue_os_filed = bool(issue_url)
            if issue_url and decision.allowed:
                fix_wo_id = str(issues.start_work(
                    issue_url, project=project.name,
                    metadata=steward.stamped({}, decision),
                    budget_usd=decision.budget_usd).get("wo_id") or "")
            elif not cleared and not issue_url:
                filed = bugreport.report_bug(
                    title=f"`{fact.get('base') or 'main'}` is red in {repo}",
                    description=self._base_red_brief(project, fact),
                    expected=(f"`{fact.get('base') or 'main'}` is green, so the OS can "
                              f"merge the pull requests it is holding"),
                    actual=(f"workflow `{fact.get('workflow') or 'ci'}` fails on "
                            f"{fact.get('run_url') or 'an unreadable run'}"),
                    # §2: `expedite` is what dispatches
                    priority="high", expedite=decision.allowed, project=project.name)
                issue_url = str(filed.get("url") or "")
                fix_wo_id = str((filed.get("pickup") or {}).get("wo_id") or "")
                issue_os_filed = bool(issue_url)
                if fix_wo_id:
                    self._stamp(store, fix_wo_id, decision)
        except Exception:  # noqa: BLE001 — every failure here is transient and retried
            log.exception("[%s] could not dispatch a fix for the red default branch",
                          project.name)
        # Whatever DID succeed, so an issue filed with no work order is not filed twice and the next tick...
        if (issue_url, fix_wo_id, issue_os_filed) != (
                fact.get("issue_url"), fact.get("fix_wo_id"),
                fact.get("issue_os_filed")):
            self.central.set_base_health(
                project.name,
                {**fact, "issue_url": issue_url, "fix_wo_id": fix_wo_id,
                 "issue_os_filed": issue_os_filed})
        if fix_wo_id:
            steward.linked(decision, fix_wo_id)
            log.info("[%s] %s is fixing the red default branch (%s)", project.name,
                     fix_wo_id, issue_url)

    def _base_red_outlived_grace(self, project: ProjectSpec) -> bool:
        """Has the open red episode outlived `base_fix_grace_minutes`?"""
        episodes = self.central.base_red_episodes(project.name)
        if not episodes or episodes[-1][1] is not None:
            return False  # no open episode yet: the transition tick records it
        started = float(episodes[-1][0] or 0)
        grace = project.fleet_health.base_fix_grace_minutes * 60.0
        return db.now() - started >= grace

    @staticmethod
    def _stamp(store: ProjectStore, wo_id: str, decision: Any) -> None:
        """Put a steward decision's provenance on an order something else created"""
        try:
            wo = store.get_work_order(wo_id)
        except KeyError:
            return
        meta = db.from_json(wo.get("metadata"), {}) or {}
        store.update_work_order(wo_id, metadata=db.to_json(steward.stamped(meta, decision)))

    def _adopt_base_red_issue(self, repo: str, run_id: Any) -> str:
        """An OPEN issue THE OS ITSELF filed citing this failing run, or `""` — spec §4"""
        from . import bugreport

        # Shared with `jarvis bug report`'s own dedupe, so both sides key a break alike (issue 1212)
        return bugreport.open_os_filed_issue(repo, run_id)

    @staticmethod
    def _base_red_brief(project: ProjectSpec, fact: dict[str, Any]) -> str:
        """The dispatched worker's evidence — and the next sweep's adoption key (§4)"""
        blamed = str(fact.get("wo_id") or "")
        return (f"`{fact.get('base') or 'main'}` is red in {project.name}. Workflow "
                f"`{fact.get('workflow') or 'ci'}` fails on run "
                f"{fact.get('run_url') or 'an unreadable run'} (run id "
                f"{fact.get('run_id') or 0}), at commit "
                f"{fact.get('head_sha') or 'an unknown commit'}"
                + (f", merged by the OS for {blamed}" if blamed else "")
                + ". Nothing merges onto a broken default branch, so every parked pull "
                  "request in the fleet is held until this lands. Revert or forward fix "
                  "— that call is yours.")

    def _attribute_base_red(self, project: ProjectSpec, store: ProjectStore,
                            owner: str, repo: str, sha: str) -> str:
        """Which work order's merge broke the base?"""
        from . import ci, github

        try:
            subject = ci.commit_subject(owner, repo, sha, cwd=project.path)
        except github.GitHubError as e:
            log.debug("[%s] could not read %s's subject: %s", project.name, sha, e)
            return ""
        numbers = re.findall(r"\(#(\d+)\)", subject)
        if not numbers:
            return ""
        # The LAST one: a squash subject re-quotes the issue it closes before its own number (#749)
        for wo in store.work_orders_for_pr_number(int(numbers[-1])):
            if store.events_of_kind(wo["id"], "automerge_merged"):
                return str(wo["id"])
        return ""

    def _base_red(self, project: ProjectSpec) -> Any:
        """The stored red-base fact as `automerge.BaseRed`, or None"""
        from . import automerge
        from .central_store import BASE_HEALTH_FRESH_SECONDS

        try:
            fact = self.central.base_health(project.name)
        except Exception:  # noqa: BLE001 — an unreadable fact is not a red base
            return None
        if not fact.get("red"):
            return None
        checked_at = fact.get("checked_at")
        if not isinstance(checked_at, (int, float)):
            return None
        if db.now() - float(checked_at) > BASE_HEALTH_FRESH_SECONDS:
            return None
        return automerge.BaseRed(
            base=str(fact.get("base") or ""), workflow=str(fact.get("workflow") or ""),
            run_url=str(fact.get("run_url") or ""),
            head_sha=str(fact.get("head_sha") or ""),
            wo_id=str(fact.get("wo_id") or ""),
            # Spec 2026-10-14-a-red-main-dispatches-its-own-fix.md §5
            fix_wo_id=str(fact.get("fix_wo_id") or ""),
            issue_url=str(fact.get("issue_url") or ""),
            issue_os_filed=bool(fact.get("issue_os_filed")))

    def poll_pull_requests(self, project: ProjectSpec, store: ProjectStore) -> None:
        """Ask GitHub what happened to the pull requests this project is parked behind (docs/superpowers/specs/2026-08-22-a-work-order-heals-its-own-pull-request.md)"""
        candidates = [wo for wo in store.list_work_orders(statuses=PR_POLL_STATUSES,
                                                          include_hidden=True)
                      if wo.get("pr_url")]
        if not candidates:
            return
        from . import github, ops

        # GATE-ONLY COLUMNS OUT, AND FOR ONE STATEMENT PER STEP
        recorded = store.work_orders_with_event("pr_url_recorded",
                                                [wo["id"] for wo in candidates])
        parked = [wo for wo in candidates if polls_pull_request(store, wo,
                                                                recorded=recorded)]
        if not parked:
            return

        # THE BASE'S CI, READ ONCE PER PROJECT PER TICK AND LAZILY
        base_ci: dict[str, tuple[Any, ...] | None] = {}
        # THE REMOTE'S OWN BRANCH TIPS, read once per project per tick and lazily
        tips: dict[str, str] | None = None
        # THE RED-BASE FACT, READ ONCE PER PROJECT PER TICK
        base_red = self._base_red(project) if (project.validation.enabled
                                               and project.validation.auto_merge) else None

        for wo in parked:
            try:
                pr = github.pr_view(wo["pr_url"], cwd=project.path)
            except github.GitHubError as e:
                # One unreadable pull request must not hide the others: a deleted repo or a typo'd URL is a...
                log.debug("[%s] could not read %s for %s: %s", project.name,
                          wo["pr_url"], wo["id"], e)
                self._warn_pr_poll_broken(project, store, e)
                continue
            except Exception:  # noqa: BLE001 — never let one work order stall the rest
                log.exception("[%s] polling %s failed", project.name, wo["id"])
                continue
            # THE HEAD, CACHED (docs/superpowers/specs/2026-09-28-stale-blockers-outlive-what-settled-them.md)
            unknown = (pr.mergeable == "UNKNOWN"
                       or (pr.mergeable is None and pr.state == "OPEN"))
            tip = ""
            if unknown and pr.head_ref:
                if tips is None:
                    try:
                        tips = github.ls_remote(cwd=project.path)
                    except Exception:  # noqa: BLE001 — stale beats wrong, and one
                        # unreadable remote must not break the poll for other orders
                        log.debug("[%s] could not list remote heads", project.name)
                        tips = {}
                tip = tips.get(pr.head_ref, "")
            cache: dict[str, Any] = {}
            if pr.head_oid and pr.head_oid != str(wo.get("pr_head_oid") or ""):
                # VOID THE TIP ON THE SAME WRITE: it is only re-read while mergeability is UNKNOWN
                cache.update(pr_head_oid=pr.head_oid, pr_head_seen_at=db.now(),
                             pr_ref_oid="")
            since = wo.get("pr_mergeable_unknown_since")
            if unknown and not isinstance(since, (int, float)):
                cache["pr_mergeable_unknown_since"] = db.now()
            elif not unknown and isinstance(since, (int, float)):
                cache["pr_mergeable_unknown_since"] = None
            if tip and tip != cache.get("pr_ref_oid", str(wo.get("pr_ref_oid") or "")):
                cache["pr_ref_oid"] = tip
            if cache:
                store.update_work_order(wo["id"], **cache)
                # THE EPISODE'S END, and only on a tick where a stall column actually cleared — wo-7ec57748
                if (("pr_mergeable_unknown_since" in cache
                     and cache["pr_mergeable_unknown_since"] is None)
                        or cache.get("pr_ref_oid") == "") \
                        and pr_resync_open(store, wo):
                    store.add_event(wo["id"], PR_RESYNC_CLEARED_EVENT,
                                    {"pr_url": wo.get("pr_url"),
                                     "head_oid": pr.head_oid or ""})
            # THE PREDICATE IS EXECUTED AND NOT MERELY WRITTEN
            stalling = {**wo, **cache}
            # WAS THE RESYNC PERFORMED ON THIS VERY TICK?
            resynced = False
            if pr.state == "OPEN" and pr_recompute_stalled(store, stalling):
                self._note_recompute_stalled(store, stalling, pr)
                # ...AND REPAIR IT, ONCE PER EPISODE (wo-7ec57748)
                if wo["status"] in PR_REPAIR_STATUSES and pr_resync_owed(store, wo):
                    self.resync_pull_request(project, store, stalling, pr)
                    resynced = True
            # A CANCELLED CHECK IS RE-RUN ONCE PER HEAD, then handed to the user (#1123)
            try:
                self.rerun_aborted_checks(project, store, stalling, pr)
            except Exception:  # noqa: BLE001 — a re-run must never stall the poll
                log.exception("[%s] re-running cancelled CI for %s failed", project.name,
                              wo["id"])
            try:
                if pr.merged:
                    # THE ROUND FIRST, if one is open: the pull request has landed
                    ops.void_round_for_settled_pr(
                        store, wo, "the pull request merged while this round was open")
                    # Deliberately silent
                    log.info("[%s] %s merged — completing %s", project.name,
                             wo["pr_url"], wo["id"])
                    ops.complete_merged(store, wo, merged_at=pr.merged_at,
                                        head_oid=pr.head_oid,
                                        merge_commit=pr.merge_commit_oid)
                elif pr.closed_unmerged:
                    # ONCE PER CLOSURE
                    if pr_resync_unreopened(store, wo):
                        log.info("[%s] %s is closed because the OS closed it to resync "
                                 "it — %s is not refused work", project.name,
                                 wo["pr_url"], wo["id"])
                    elif not store.pr_closure_told(wo["id"]):
                        log.info("[%s] %s closed unmerged — %s needs the user",
                                 project.name, wo["pr_url"], wo["id"])
                        # Same reason as the merge above
                        ops.void_round_for_settled_pr(
                            store, wo,
                            "the pull request was closed while this round was open")
                        ops.record_pr_closed(store, wo)
                elif wo["status"] not in PR_REPAIR_STATUSES:
                    # `validating`, and the pull request is neither merged nor closed
                    pass
                elif self._note_reopened(project, store, wo):
                    # The pull request is OPEN and the record still says it was refused
                    log.info("[%s] %s is open again — %s", project.name,
                             wo["pr_url"], wo["id"])
                elif not resynced and self._note_resync_reopened(project, store, wo, pr):
                    # THE RESYNC WINDOW'S END, and the position is most of the guard: this chain is bounded by...
                    pass
                elif pr.conflicting:
                    self.heal_pull_request(project, store, wo, ops.PR_CONFLICT,
                                           "conflicts",
                                           base=pr.base_ref or "its base branch")
                    # ...and say what is holding the merge NOW (#263)
                    self.auto_merge(project, store, wo, pr, record_only=True,
                                    base_red=base_red)
                elif pr.failing:
                    # IS THIS THE BRANCH'S FAILURE AT ALL?
                    handled, pr = self.heal_inherited_failure(project, store, wo, pr,
                                                              base_ci)
                    if not handled:
                        self.heal_pull_request(
                            project, store, wo, ops.PR_CHECKS,
                            f"has failing checks ({', '.join(pr.failing)})",
                            failing=", ".join(pr.failing),
                            # BEHIND rides along with a nudge that was going out anyway and never causes one: spec §5
                            behind=(ops.PR_BEHIND_NOTE.format(
                                base=pr.base_ref or "its base") if pr.behind else ""))
                    self.auto_merge(project, store, wo, pr, record_only=True,
                                    base_red=base_red)
                else:
                    healed = pr.mergeable_now and ops.clear_pr_repair(
                        store, wo, ops.PR_CONFLICT)
                    if healed:
                        log.info("[%s] %s merges again — %s stopped conflicting",
                                 project.name, wo["pr_url"], wo["id"])
                    # Not `elif`: a pull request can stop conflicting and go green in the same poll
                    if pr.checks_green:
                        # Re-read ONLY if the clear above ran
                        row = store.get_work_order(wo["id"]) if healed else wo
                        if ops.clear_pr_repair(store, row, ops.PR_CHECKS):
                            log.info("[%s] %s is green again — %s stopped failing",
                                     project.name, wo["pr_url"], wo["id"])
                        # ...and take down "waiting for main to go green" if it is up
                        ops.record_base_health(store, wo, red=False,
                                               base=pr.base_ref or "its base")
                    # AFTER the repairs clear, and inside the same branch: a pull request the OS is still nudging a...
                    self.auto_merge(project, store, wo, pr, base_red=base_red)
                # AN UNDECLARED DELIVERY IS ORTHOGONAL TO THE CHAIN ABOVE (docs/superpowers/specs/2026-10-07-an-undeclared-delivery-must-heal-itself.md §2.3.)
                row = {**wo, "pr_head_oid": pr.head_oid or wo.get("pr_head_oid") or ""}
                if wo["status"] in PR_REPAIR_STATUSES \
                        and invariants_mod.undeclared_delivery(store, row):
                    self.heal_pull_request(project, store, row, ops.PR_UNDECLARED,
                                           "delivered without declaring it")
                elif wo["status"] in PR_REPAIR_STATUSES:
                    ops.clear_pr_repair(store, row, ops.PR_UNDECLARED)
            except Exception:  # noqa: BLE001
                log.exception("[%s] settling %s against its PR failed", project.name,
                              wo["id"])

    def attach_pull_requests(self, project: ProjectSpec, store: ProjectStore) -> None:
        """Record the open pull request the user made for a pushed-branch park; routes as `auto` (docs/superpowers/specs/2026-10-12-a-pushed-branch-with-no-pull-request-is-a-state.md §9)"""
        from . import github, ops

        # One bulk statement per step, `poll_pull_requests`' shape
        candidates = [wo for wo in store.list_work_orders(statuses=("needs_review",),
                                                          include_hidden=True)
                      if not wo.get("pr_url")]
        if not candidates:
            return
        ever = store.work_orders_with_event(PR_PENDING_EVENT,
                                            [wo["id"] for wo in candidates])
        parked = [(wo, park) for wo in candidates if wo["id"] in ever
                  and (park := store.pr_pending_open(wo["id"])) is not None]
        if not parked:
            return
        for wo, park in parked:
            branch = str(park.get("branch") or "")
            head_sha = str(park.get("head_sha") or "")
            if not branch or not head_sha:
                continue
            try:
                url = github.open_pull_request_for_branch(branch, cwd=project.path,
                                                          head_sha=head_sha)
            except github.GitHubError as e:
                log.debug("[%s] could not look for %s's pull request: %s", project.name,
                          wo["id"], e)
                return
            if not url:
                continue
            try:
                ops.record_pull_request(wo["id"], url, project_name=project.name,
                                        source="auto")
            except ops.OpsError:
                log.exception("[%s] could not attach %s to %s", project.name, url,
                              wo["id"])
                continue
            log.info("[%s] attached %s to %s — found open on %s", project.name, url,
                     wo["id"], branch)

    def refresh_landings(self, project: ProjectSpec, store: ProjectStore) -> None:
        """Ask GitHub what became of the pull request each COMPLETED order recorded"""
        from . import github, landing

        candidates = [wo for wo in store.list_work_orders(
            statuses=("completed",), include_hidden=True)
            if wo.get("pr_url") and not (
                store.work_abandoned(wo["id"])
                or store.work_unlanded_open(wo["id"], closed_by="marked_done"))]
        stale = [wo for wo in candidates if self._needs_landing_refresh(store, wo["id"])]
        for wo in stale[:landing.REFRESH_PER_SWEEP]:
            wo_id = wo["id"]
            try:
                pr = github.pr_view(str(wo["pr_url"]), cwd=project.path)
            except github.GitHubError as e:
                log.debug("[%s] could not read the pull request of %s: %s", project.name,
                          wo_id, e)
                self._warn_pr_poll_broken(project, store, e)
                # Continue, unlike the branch sweep this replaced: here each order asks about a DIFFERENT url
                continue
            except Exception:  # noqa: BLE001 — one work order must not stall the rest
                log.exception("[%s] refreshing the landing of %s failed", project.name,
                              wo_id)
                continue
            found = landing.judge(wo_id, str(wo["pr_url"]), pr.state)
            store.add_event(wo_id, "landing_seen", found.record())
            # THE MERGE ITSELF, as an event and never as `pr_state`
            if pr.state == "MERGED" and not store.events_of_kind(wo_id, "pr_merged"):
                store.add_event(wo_id, "pr_merged", {
                    "pr_url": str(wo["pr_url"]), "head_oid": pr.head_oid,
                    "merged_at": pr.merged_at, "source": "landing_sweep"})
            log.debug("[%s] %s: %s", project.name, wo_id, found.detail)

    def _needs_landing_refresh(self, store: ProjectStore, wo_id: str) -> bool:
        """Is this completed order's landing record missing or old enough to re-ask?"""
        from . import landing

        seen = store.events_of_kind(wo_id, "landing_seen")
        if not seen:
            return True
        latest = seen[-1]
        if landing.from_record(wo_id, db.from_json(latest["payload"], {})).unsettled:
            return True
        return db.now() - float(latest["ts"] or 0.0) >= landing.FRESH_FOR_SECONDS

    def auto_merge(self, project: ProjectSpec, store: ProjectStore, wo: dict,
                   pr: Any, *, record_only: bool = False,
                   base_red: Any = None) -> None:
        """Merge this pull request, if six positive facts line up (docs/superpowers/specs/2026-09-14-validated-auto-merge-design.md)"""
        from . import automerge, branchproof, ops

        cfg = project.validation
        if not (cfg.enabled and cfg.auto_merge):
            return
        if wo["status"] != "waiting_pr_merge":
            return
        wo_id = wo["id"]
        # ONE read of the round, used twice: `ProjectStore.validated_head` turns it into the predicate and...
        round_row = store.latest_validation_round(wo_id=wo_id)
        pending = bool(store.pending_assumptions(wo_id))
        # ONE read, passed to both readers below (docs/superpowers/specs/2026-09-24-a-planner-assumption-holds-its-feature.md §2.4)
        hold = store.plan_hold(wo)
        plan_assumptions = str((hold or {}).get("planner_id") or "")
        # docs/superpowers/specs/2026-10-29-a-feature-spec-pull-request-lands-first.md §2
        spec_unmerged = str((store.spec_hold(wo) or {}).get("pr_url") or "")
        # Spec 2026-09-28 §3.2
        base_behind = (automerge.BaseBehind(base=pr.base_ref, base_oid=pr.base_oid)
                       if ops.catch_up_needed(pr, repo=project.path) else None)
        # #1063
        drift = self._drift_hold(store, wo)
        decision = automerge.decide(
            round_row, wo, pr, cfg,
            validated_head=store.validated_head(round_row),
            pending_assumptions=pending, plan_assumptions=plan_assumptions,
                    spec_unmerged=spec_unmerged,
            drift=drift, base_red=base_red, base_behind=base_behind)
        if not decision.armed:
            # A CATCH-UP WITH THE BASE COSTS NO ROUND (spec... (docs/superpowers/specs/2026-09-27-a-catch-up-with-main-costs-no-round.md §3.5)
            outcome = CarryOutcome(False, False)
            if not record_only and decision.code == automerge.HELD_SHA_MOVED:
                outcome = self._carry_catch_up(
                    project, store, wo, pr, judged=decision.judged_sha,
                    head=decision.head_sha, round_n=decision.round_n)
            if outcome.carried:
                round_row = store.latest_validation_round(wo_id=wo_id)
                decision = automerge.decide(
                    round_row, wo, pr, cfg,
                    validated_head=store.validated_head(round_row),
                    pending_assumptions=pending, plan_assumptions=plan_assumptions,
                    spec_unmerged=spec_unmerged,
                    drift=drift)
            if not decision.armed and not record_only \
                    and decision.code == automerge.HELD_BASE_MOVED:
                # THE HOLD IS WHERE THE CATCH-UP HANGS NOW (spec 2026-09-28 §3.2
                caught = self._catch_up_with_base(project, store, wo, pr)
                if (caught.outcome == ops.CATCH_UP_DEFERRED
                        and caught.cause == ops.CATCH_UP_DEFER_HEAD_MOVED):
                    # #1188: THE OS'S OWN CATCH-UP MOVED THE HEAD, and this hold outranks the...
                    carry = self._carry_catch_up(
                        project, store, wo, pr,
                        judged=store.validated_head(round_row) or "",
                        head=str(getattr(caught.pr, "head_oid", "") or ""),
                        round_n=int((round_row or {}).get("round") or 0))
                    if carry.carried:
                        round_row = store.latest_validation_round(wo_id=wo_id)
                        caught = self._catch_up_with_base(project, store, wo, pr)
                    else:
                        # A HOLD NOTHING CAN PROVE IS AN ASK: `invariants.carry_unproven_hold`
                        caught = dataclasses.replace(
                            caught, cause=ops.CATCH_UP_DEFER_CARRY_UNPROVEN)
                if caught.outcome == ops.CATCH_UP_NOT_NEEDED:
                    # THE FETCHED READ WINS, AND NOT DECIDING HERE WOULD STALL FOR EVER
                    base_behind = None
                    log.debug("[%s] %s: the fetched base contradicts the poll's reading "
                              "— base_oid %s, behind=%s, and the head already contains "
                              "the tip; deciding without it", project.name, wo_id,
                              str(getattr(pr, "base_oid", "") or "")[:10] or "unknown",
                              bool(getattr(pr, "behind", False)))
                    decision = automerge.decide(
                        round_row, wo, pr, cfg,
                        validated_head=store.validated_head(round_row),
                        pending_assumptions=pending, plan_assumptions=plan_assumptions,
                    spec_unmerged=spec_unmerged,
                        drift=drift, base_red=base_red)
                    # #1141: the moved head the base hold was hiding gets the carry too
                    if not decision.armed and decision.code == automerge.HELD_SHA_MOVED:
                        outcome = self._carry_catch_up(
                            project, store, wo, pr, judged=decision.judged_sha,
                            head=decision.head_sha, round_n=decision.round_n)
                        if outcome.carried:
                            round_row = store.latest_validation_round(wo_id=wo_id)
                            decision = automerge.decide(
                                round_row, wo, pr, cfg,
                                validated_head=store.validated_head(round_row),
                                pending_assumptions=pending,
                                plan_assumptions=plan_assumptions,
                    spec_unmerged=spec_unmerged, drift=drift,
                                base_red=base_red)
                else:
                    # A PENDING REQUEST IS WITHDRAWN (spec §3.6): it was filed against a base that has moved
                    moved = bool(caught.base_tip) and ops.catch_up_needed(
                        pr, repo=project.path, base_tip=caught.base_tip)
                    judged = store.validated_head(round_row) or ""
                    filed = (store.latest_approval_for(
                        wo_id, automerge.GATE_KIND,
                        automerge.merge_command(str(wo.get("pr_url") or ""), judged))
                        if judged else None)
                    if (moved and caught.outcome != ops.CATCH_UP_DEFERRED
                            and filed is not None
                            and filed["status"] in ("pending", "awaiting_case")):
                        self._supersede_stale_request(store, filed, pr,
                                                      base_tip=caught.base_tip)
                    # The FETCHED tip, never `pr.base_oid` — spec 2026-09-28 §3.1
                    self._note_automerge_held(
                        store, wo_id,
                        automerge.held_base_moved(caught, pr, caught.base_tip))
                    return
            if not decision.armed:
                self._note_automerge_held(store, wo_id, decision)
                if not record_only and decision.code == automerge.HELD_DRAFT:
                    automerge.mark_ready(store, wo, decision.head_sha, cwd=project.path)
                # TWO CODES, ONE PROPERTY (spec 2026-10-16 §1): a passed round whose commit binding is unusable and a...
                if (not record_only
                        and decision.code in (automerge.HELD_SHA_MOVED,
                                              automerge.HELD_SHA_UNRECORDED)
                        and automerge.only_the_head_moved(
                            round_row, wo, pr, cfg, pending_assumptions=pending,
                            plan_assumptions=plan_assumptions,
                    spec_unmerged=spec_unmerged)):
                    # `rebind` is the carry's reading of the two proofs
                    self._rejudge_moved_head(project, store, wo, decision,
                                             rebind=outcome.rebind)
                return
        if record_only:
            return          # unreachable: a repairing pull request cannot arm, see above

        approval = store.latest_approval_for(
            wo_id, automerge.GATE_KIND,
            automerge.merge_command(str(wo.get("pr_url") or ""), decision.judged_sha))
        if approval is None:
            # BEFORE A GRANT EXISTS AND NEVER AFTER ONE (spec 2026-09-27 §5.2)
            caught = self._catch_up_with_base(project, store, wo, pr)
            if caught.outcome != ops.CATCH_UP_NOT_NEEDED:
                # The FETCHED tip, never `pr.base_oid` — spec 2026-09-28 §3.1
                self._note_automerge_held(
                    store, wo_id,
                    automerge.held_base_moved(caught, pr, caught.base_tip))
                return
        if approval is not None and approval["status"] != "approved":
            # A PENDING REQUEST WHOSE BASE HAS MOVED IS SUPERSEDED
            if (approval["status"] in ("pending", "awaiting_case")
                    and base_behind is not None):
                self._supersede_stale_request(store, approval, pr)
                return
            # Filed and not granted: pending with Neo, escalated to the user, denied, expired
            return
        if approval is None:
            # Not yet permitted
            from .neo_store import NeoStore

            neo_store = NeoStore()
            try:
                automerge.propose(store, neo_store, project.name, wo, decision,
                                  # Whole, with each conclusion — spec 2026-09-24 fix 2
                                  checks=pr.checks,
                                  protection=self._protection_fact(project, pr),
                                  # The base it was filed against — spec 2026-09-28 §3.5
                                  pr=pr)
            finally:
                neo_store.close()
            return
        # AT THE CAP THE ATTEMPT CAN ONLY REFUSE (#1173)
        if (ops.catch_up_attempts(store, wo_id)
                >= ops.catch_up_policy(store).catch_up_max):
            caught = self._catch_up_with_base(project, store, wo, pr)
            if caught.outcome != ops.CATCH_UP_NOT_NEEDED:
                self._note_automerge_held(
                    store, wo_id,
                    automerge.held_base_moved(caught, pr, caught.base_tip))
                return
        # No attempt cap here: `automerge.GRANT_USES` is 1
        try:
            merged = automerge.apply(store, wo, decision.judged_sha, approval,
                                     cwd=project.path, base_ref=pr.base_ref)
        except automerge.StaleBase as exc:
            # THE BASE MOVED UNDER THE JUDGED COMMIT (spec 2026-09-28 §3.1)
            log.info("[%s] auto-merge of %s refused: %s", project.name, wo_id, exc)
            tip = branchproof.tip(project.path, f"origin/{pr.base_ref}")
            if tip not in ops.stale_base_bases(store, wo_id, decision.judged_sha):
                store.add_event(wo_id, ops.MERGE_BASE_STALE_EVENT, {
                    "head_sha": decision.judged_sha, "base": pr.base_ref,
                    "base_oid": tip, "round": decision.round_n,
                    "approval_id": approval["id"],
                    "reason": str(exc), "pr_url": wo.get("pr_url")})
            caught = self._catch_up_with_base(project, store, wo, pr)
            self._note_automerge_held(store, wo_id,
                                      automerge.held_base_moved(caught, pr, tip))
            return
        except automerge.MergeFailed as exc:
            # It reached GitHub and GitHub said no. Counted, recorded and — once per commit — reported
            log.warning("[%s] auto-merge of %s failed: %s", project.name, wo_id, exc)
            store.add_event(wo_id, "automerge_failed", {
                "head_sha": decision.judged_sha, "approval_id": approval["id"],
                "reason": str(exc), "pr_url": wo.get("pr_url")})
            self._warn_automerge_failed(project, store, wo, decision, str(exc))
            return
        except automerge.AutoMergeRefused as exc:
            # NOTHING WAS ATTEMPTED, so nothing is recorded and nothing is counted
            log.debug("[%s] auto-merge of %s not attempted: %s", project.name, wo_id,
                      exc)
            return
        except Exception:  # noqa: BLE001 — a merge must never kill the tick
            log.exception("[%s] auto-merging %s failed", project.name, wo_id)
            return
        log.info("[%s] auto-merged %s — completing %s", project.name, wo["pr_url"],
                 wo_id)
        if merged["after_merge_cause"]:
            # THE MERGE LANDED AND THE COMMAND THAT MADE IT DID NOT SUCCEED (issue #253, spec §5.5)
            store.add_event(wo_id,
                            automerge.AFTER_MERGE_EVENT[merged["after_merge_cause"]], {
                "head_sha": decision.judged_sha, "approval_id": approval["id"],
                "reason": merged["after_merge_error"], "pr_url": wo.get("pr_url")})
        # No `merged_at`: that column holds GITHUB's `mergedAt` (#232)
        ops.complete_merged(store, wo, head_oid=decision.judged_sha,
                            automerge={"approval_id": merged["approval_id"],
                                       "round_id": decision.round_id,
                                       "round": decision.round_n,
                                       "head_sha": decision.judged_sha})

    def auto_review(self, project: ProjectSpec, store: ProjectStore) -> None:
        """Put this project's pending assumptions to Neo, one question each (docs/superpowers/specs/2026-09-15-neo-decides-an-assumption.md)"""
        cfg = project.validation
        if not (cfg.enabled and cfg.auto_review):
            return
        if not self.catalog.os.neo.enabled:
            return
        # BEFORE the candidate list and outside it
        self._file_pending_objections(project, store)
        from .neo_store import NeoStore

        # Hidden orders are excluded by the default
        candidates: list[tuple[dict, list[dict], bool]] = []
        for early, status in ((False, "needs_review"), (True, "running")):
            for wo in store.list_work_orders(statuses=(status,)):
                if wo.get("kind") == "investigator":
                    # A diagnosis is not a submission and nothing is gated on it: §2.3 of...
                    continue
                rows = store.all_assumptions(wo["id"])
                if any(a["status"] == "pending"
                       and not (early and a.get("provisional_verdict"))
                       for a in rows):
                    candidates.append((wo, rows, early))
        if not candidates:
            return
        # A sqlite connection belongs to the thread that made it (`_validate_work_order`'s reason)
        neo_store = NeoStore()
        try:
            for wo, assumptions, early in candidates:
                try:
                    self._review_assumptions_of(project, store, neo_store, wo, cfg,
                                                assumptions, early=early)
                except Exception:  # noqa: BLE001 — one order must never stop the rest
                    log.exception("[%s] auto-review of %s failed", project.name,
                                  wo["id"])
        finally:
            neo_store.close()

    def _file_pending_objections(self, project: ProjectSpec,
                                 store: ProjectStore) -> None:
        """Send the early pass's disagreements to the workers still typing (§6.1)"""
        from . import ops

        for wo in store.list_work_orders(statuses=("running",)):
            try:
                for a in store.all_assumptions(wo["id"]):
                    if (str(a.get("provisional_verdict") or "") != "object"
                            or a.get("objection_envelope_id") is not None
                            or a.get("objection_withdrawn_ts") is not None):
                        continue
                    ops.file_assumption_objection(
                        store, project.path, wo, a,
                        reason=str(a.get("provisional_reason") or ""),
                        model=str(a.get("provisional_model") or ""),
                        question_id=a.get("neo_question_id"))
            except Exception:  # noqa: BLE001 — one order must never stop the rest
                log.exception("[%s] objections for %s could not be filed",
                              project.name, wo["id"])

    def _stamp_objection_delivery(self, store: ProjectStore, msg_id: int) -> None:
        from . import ops
        ops.stamp_objection_delivery(store, msg_id)

    def withdraw_stale_objections(self, project: ProjectSpec,
                                  store: ProjectStore) -> None:
        """Take back every objection whose order will never take another turn (§6.6)"""
        for wo_id in store.work_orders_with_objections():
            try:
                status = str(store.get_work_order(wo_id)["status"] or "")
                # TERMINAL, never merely not-running: `needs_review`
                if status not in TERMINAL_STATUSES and status != "needs_review":
                    continue
                reason = (OBJECTION_HANDED_BACK_REASON if status == "needs_review"
                          else OBJECTION_WITHDRAWN_REASON)
                for a in store.outstanding_objections(wo_id):
                    self._withdraw_objection(store, wo_id, assumption=a, reason=reason)
            except Exception:  # noqa: BLE001 — one order must never stop the rest
                log.exception("[%s] objections for %s could not be withdrawn",
                              project.name, wo_id)

    def check_objection_withdrawals(self, project: ProjectSpec,
                                    store: ProjectStore) -> None:
        """Withdrawals past `objection_withdrawal_alarm_rate` are the feature failing: inbox, once a window"""
        rate = float(getattr(project.validation, "objection_withdrawal_alarm_rate", 1.0))
        if rate >= 1.0:
            return
        since = time.time() - OBJECTION_ALARM_WINDOW_DAYS * 86400
        counts = store.objection_counts(since=since)
        sent, withdrawn = int(counts["sent"]), int(counts["withdrawn"])
        if sent < OBJECTION_ALARM_MIN_SENT or withdrawn / sent <= rate:
            return
        title = f"Most of the OS's early objections on {project.name} never reached a worker"
        if self.central.inbox_titled_since(project.name, title, since):
            return
        self.central.add_inbox(
            project=project.name, level="warning", title=title,
            body=f"{withdrawn} of {sent} objections sent in the last "
                 f"{OBJECTION_ALARM_WINDOW_DAYS} days were withdrawn undelivered — "
                 f"above this project's validation.objection_withdrawal_alarm_rate "
                 f"({rate:.0%}). Neo's early reading is not reaching the work.\n"
                 f"Sent / delivered / acted on / withdrawn: jarvis neo stats --project "
                 f"{project.name} --days {OBJECTION_ALARM_WINDOW_DAYS}")

    def _withdraw_objection(self, store: ProjectStore, wo_id: str, *,
                            assumption: dict[str, Any] | None = None,
                            envelope: dict[str, Any] | None = None,
                            reason: str = OBJECTION_WITHDRAWN_REASON) -> None:
        """Disarm the carrier FIRST, then stamp the withdrawal (§6.6, in that order)"""
        if envelope is None and assumption and assumption.get("objection_envelope_id"):
            envelope = store.get_envelope(int(assumption["objection_envelope_id"]))
        if assumption is None and envelope is not None:
            assumption = next(
                (a for a in store.outstanding_objections(wo_id)
                 if a.get("objection_envelope_id") == envelope["id"]), None)
        if envelope is not None:
            if envelope["state"] == "queued":
                store.mark_envelope(int(envelope["id"]), "withdrawn", note=reason)
            if envelope.get("delivered_msg_id"):
                # ONLY FROM `queued`, the envelope's own rule: a delivered message was read by the worker and...
                msg = store.get_message(int(envelope["delivered_msg_id"]))
                if msg and msg["status"] == "queued":
                    store.mark_message(int(envelope["delivered_msg_id"]), "withdrawn")
        if assumption is None:
            return  # the carrier is disarmed and nothing is outstanding to stamp
        store.withdraw_objection(int(assumption["id"]))
        store.add_event(wo_id, "autoreview_objection_withdrawn", {
            "assumption_id": assumption["id"], "n": assumption.get("n"),
            # THE CARRIER OF THE REASON (§3): the envelope note is written only from `queued`
            "reason": reason,
            "envelope_id": assumption.get("objection_envelope_id")})

    def _unreachable_question_ids(self, neo_store: Any,
                                  assumptions: list[dict]) -> set[int]:
        """The links among these rows that point at a question NOBODY WILL EVER ANSWER"""
        return self._question_liveness(neo_store, assumptions)[0]

    def _question_liveness(self, neo_store: Any,
                           assumptions: list[dict]) -> tuple[set[int], set[int]]:
        """`(dead, open)` for every question these rows link to"""
        from .neo_store import NEO_HELD_Q_STATUSES

        dead: set[int] = set()
        live: set[int] = set()
        seen: set[int] = set()
        for a in assumptions:
            for column in ("neo_question_id", "confirm_question_id"):
                qid = int(a.get(column) or 0)
                if not qid or qid in seen:
                    continue
                seen.add(qid)
                q = neo_store.get(qid)
                if q is None:
                    continue
                status = str(q["status"] or "")
                if status == "failed":
                    dead.add(qid)
                if status in NEO_HELD_Q_STATUSES:
                    live.add(qid)
        return dead, live

    def _review_assumptions_of(self, project: ProjectSpec, store: ProjectStore,
                               neo_store: Any, wo: dict, cfg: Any,
                               assumptions: list[dict], *, early: bool = False) -> None:
        """One work order's assumptions, each decided on its own"""
        from . import autoreview, ops

        # ONE read of the round and one of the refusal history for the whole list: both are facts about the...
        latest = store.latest_validation_round(wo_id=wo["id"])
        outcome = str((latest or {}).get("outcome") or "")
        # WHICH round and WHAT IT SAID travel with the outcome: the hold is the only place the user meets the...
        round_n = int((latest or {}).get("round") or 0)
        round_reason = str((latest or {}).get("reason") or "")
        answered = ops.refusal_answered(store, wo["id"])
        # ...and WHICH unanswered refusal this is (docs/superpowers/specs/2026-09-28-stale-blockers-outlive-what-settled-them.md)
        undeclared = not answered and invariants_mod.undeclared_delivery(store, wo)
        objecting = bool(store.outstanding_objections(wo["id"]))
        dead_questions, open_questions = self._question_liveness(neo_store, assumptions)
        rule = autoreview.decide_early if early else autoreview.decide
        suppress = _holds_not_recorded(early)
        for a in assumptions:
            # 2026-10-01-a-confirmation-is-not-re-run-on-a-settled-assumption.md §3.1
            if str(a.get("status") or "") != "pending":
                continue
            # `not early` is the third guard on this branch
            if not early and str(a.get("provisional_verdict") or ""):
                def confirm(stakes_verdict=None, a=a):
                    return autoreview.decide_confirm(
                        a, wo, cfg, round_outcome=outcome, round_n=round_n,
                        round_reason=round_reason, refusal_answered=answered,
                        objections_outstanding=objecting,
                        unreachable_question_ids=dead_questions,
                        # 2026-09-28 spec §6: is Neo still holding the confirmation?
                        confirmation_open=int(a.get("confirm_question_id") or 0)
                        in open_questions,
                        stakes=stakes_verdict)

                decision = self._stakes_reviewed(project, store, wo, cfg, a,
                                                 confirm(), confirm)
                if not decision.armed:
                    self._note_autoreview_held(store, wo["id"], decision,
                                               suppress=suppress)
                    continue
                # THE SECOND GATE, over the TEXTS rather than the row (docs/superpowers/specs/2026-10-14-neo-never-reads-a-diff.md)
                evidence_ok = autoreview.decide_evidence(
                    a, summary=str(wo.get("result_summary") or ""))
                if not evidence_ok.armed:
                    self._note_autoreview_held(store, wo["id"], evidence_ok,
                                               suppress=suppress)
                    continue
                try:
                    autoreview.propose_confirmation(store, neo_store, project.name, wo,
                                                    a, assumptions, cfg=cfg)
                except QuestionTooLargeError as e:
                    # The store refused to hold the question, so nothing was asked and nothing was linked (docs/superpowers/specs/2026-09-26-bounded-model-inputs.md)
                    self._note_confirmation_refused(project, store, wo, a, e)
                continue
            def judge(stakes_verdict=None, a=a):
                return rule(a, wo, cfg, round_outcome=outcome, round_n=round_n,
                            round_reason=round_reason,
                            refusal_answered=answered,
                            undeclared_delivery=undeclared,
                            unreachable_question_ids=dead_questions,
                            stakes=stakes_verdict)

            decision = self._stakes_reviewed(project, store, wo, cfg, a, judge(), judge)
            if not decision.armed:
                self._note_autoreview_held(store, wo["id"], decision,
                                           suppress=suppress)
                continue
            autoreview.propose(store, neo_store, project.name, wo, a, assumptions,
                               early=early, cfg=cfg)

    def _stakes_reviewed(self, project: ProjectSpec, store: ProjectStore, wo: dict,
                         cfg: Any, a: dict, decision: Any,
                         rerun: Callable[[Any], Any]) -> Any:
        """Apply this project's `validation.stakes_classifier` to ONE row's decision (docs/superpowers/specs/2026-09-25-a-model-decides-what-is-high-stakes.md)"""
        from . import autoreview

        mode = str(getattr(cfg, "stakes_classifier", "") or DEFAULT_STAKES_CLASSIFIER)
        if mode == "regex":
            return decision
        if not (decision.armed or decision.code == autoreview.HELD_HIGH_STAKES):
            return decision
        if mode == "regex-tightened":
            return rerun(autoreview.tightened_verdict(str(a.get("content") or "")))
        verdict = self._classify_stakes(project, wo, a)
        if mode == "shadow":
            self._note_stakes_disagreement(store, wo["id"], a, verdict)
            return decision
        return rerun(verdict)

    def _settle_stakes(self, project: ProjectSpec, wo: dict, a: dict) -> Any:
        """The verdict the SETTLE site re-checks condition 7 against, or None"""
        mode = str(getattr(project.validation, "stakes_classifier", "")
                   or DEFAULT_STAKES_CLASSIFIER)
        if mode == "regex-tightened":
            from . import autoreview
            return autoreview.tightened_verdict(str(a.get("content") or ""))
        if mode != "classifier":
            return None
        return self._classify_stakes(project, wo, a)

    def _classify_stakes(self, project: ProjectSpec, wo: dict, a: dict, *,
                         call: Callable[..., Any] | None = None,
                         record: Callable[..., Any] | None = None) -> Any:
        """Ask the classifier about one assumption"""
        from . import agent_usage, stakes

        call = call or claude_cli.run_headless_result
        record = record or agent_usage.record
        try:
            result = call(stakes.question(str(a.get("content") or "")),
                          system_prompt=stakes.PERSONA, model=stakes.MODEL,
                          timeout=stakes.TIMEOUT, cwd=ensure_home(), tools="",
                          records_itself="stakes_classifier")
        except Exception:  # noqa: BLE001 — an unreachable classifier holds, never raises
            log.exception("[%s] stakes classifier for %s failed", project.name,
                          wo.get("id"))
            record("stakes_classifier", usage=None, project=project.name,
                   wo_id=str(wo.get("id") or ""), label="assumption",
                   model=stakes.MODEL, ok=False)
            return stakes.unreachable(stakes.MODEL)
        model = getattr(result, "model", "") or stakes.MODEL
        record("stakes_classifier", usage=result, project=project.name,
               wo_id=str(wo.get("id") or ""), label="assumption", model=model,
               ok=bool(getattr(result, "text", "")))
        return stakes.read_verdict(getattr(result, "text", ""), model=model)

    def _note_stakes_disagreement(self, store: ProjectStore, wo_id: str, a: dict,
                                  verdict: Any) -> None:
        """Record ONCE that the two nets disagreed about one assumption (SS3.5)"""
        from . import autoreview, db

        marker = autoreview.high_stakes_marker(str(a.get("content") or ""))
        high = bool(getattr(verdict, "high", True))
        if bool(marker) == high:
            return
        key = (int(a.get("id") or 0), marker, high)
        for event in store.events_of_kind(wo_id, "autoreview_stakes_disagreed"):
            payload = db.from_json(event["payload"], {})
            if (int(payload.get("assumption_id") or 0), str(payload.get("regex") or ""),
                    bool(payload.get("classifier_high"))) == key:
                return
        store.add_event(wo_id, "autoreview_stakes_disagreed", {
            "assumption_id": a.get("id"), "n": a.get("n"), "regex": marker,
            "classifier_high": high, "category": verdict.category,
            "reason": verdict.reason, "model": verdict.model, "parsed": verdict.parsed})

    def _note_autoreview_held(self, store: ProjectStore, wo_id: str,
                              decision: Any, *, settling: bool = False,
                              suppress: tuple[str, ...] | None = None) -> None:
        """Record ONCE, per (assumption, reason), that the OS declined to decide"""
        if not settling and decision.code in (suppress or ()):
            return
        # THE ROUND IS PART OF THE KEY, and the hold that needs it is `panel_gave_up`: two consecutive... (#975)
        key = (int(decision.assumption_id or 0), str(decision.code or ""),
               int(decision.round or 0), str(decision.reason or ""))
        if not _hold_is_news(store, wo_id, "autoreview_held", key,
                             lambda p: (int(p.get("assumption_id") or 0),
                                        str(p.get("code") or ""),
                                        int(p.get("round") or 0),
                                        str(p.get("reason") or ""))):
            return
        store.add_event(wo_id, "autoreview_held", {
            "code": decision.code, "reason": decision.reason,
            "assumption_id": decision.assumption_id, "n": decision.n,
            # What the freshness check at the read side compares against the current round
            "round": decision.round})

    def _deliver_assumption_verdict(self, central: CentralStore, neo_store: Any,
                                    pstore: ProjectStore | None, q: dict,
                                    verdict: dict) -> None:
        """Apply Neo's ruling on ONE assumption: accept it, or leave it with the user (#833)"""
        from . import autoreview, ops

        if pstore is None:
            log.error("assumption verdict for question %s has no project store", q["id"])
            return
        assumption = pstore.assumption_for_question(q["id"])
        if assumption is None:
            log.warning("assumption question %s rules on nothing (deleted?)", q["id"])
            return
        if assumption["status"] != "pending":
            # The user got there first through `jarvis wo review`
            log.info("assumption question %s: assumption %s is already %s, dropping "
                     "Neo's ruling", q["id"], assumption["id"], assumption["status"])
            return
        try:
            wo = pstore.get_work_order(q["wo_id"])
        except KeyError:
            return
        # `n` is a position in the work order's list and is derived
        numbered = next((a for a in pstore.all_assumptions(wo["id"])
                         if a["id"] == assumption["id"]), assumption)
        if verdict.get("refine"):
            if self._asked_early(pstore, wo["id"], int(q["id"])):
                autoreview.return_to_worker(pstore, wo["id"], numbered,
                                            verdict["reason"], int(q["id"]))
                return
            # A delivered worker cannot re-file: the user settles it
            neo_store.mark(q["id"], "escalated", reason=verdict["reason"],
                           cause="scope-too-large")
            verdict = {**verdict, "refine": False, "escalate": True}
        if self._asked_early(pstore, wo["id"], int(q["id"])):
            # THE EARLY PASS ASKED THIS ONE, so its answer may only be RECORDED
            self._record_early_assumption_verdict(neo_store, pstore, wo, numbered, q,
                                                  verdict)
            return
        ruling = autoreview.read_ruling(verdict,
                                        default_model=self.catalog.os.neo.model)
        # WHICH PASS THIS VERDICT ANSWERS IS A FACT RECORDED AT ASK TIME
        confirming = int(assumption.get("confirm_question_id") or 0) == int(q["id"])
        prior = {"provisional_verdict": assumption.get("provisional_verdict") or "",
                 "provisional_reason": assumption.get("provisional_reason") or "",
                 "provisional_model": assumption.get("provisional_model") or ""}
        if not ruling.accept:
            if not verdict.get("escalate"):
                # NEO ANSWERED AND THE ANSWER IS NOT WHAT HAPPENS
                neo_store.mark(q["id"], "escalated", reason=ruling.reason,
                               cause=autoreview.escalation_cause(
                                   ruling, escalate=bool(verdict.get("escalate"))))
            payload = {
                "assumption_id": assumption["id"], "n": numbered.get("n"),
                "reason": ruling.reason, "stakes": ruling.stakes,
                "model": ruling.model, "neo_question_id": q["id"],
                "overridden": ruling.overridden}
            # An UNCONFIRMED early verdict is not an ordinary escalation and the user reads the difference: both...
            pstore.add_event(
                wo["id"], "autoreview_unconfirmed" if confirming
                else "autoreview_escalated",
                {**payload, **prior} if confirming else payload)
            log.info("auto-review left assumption #%s of %s with the user: %s",
                     numbered.get("n"), wo["id"], ruling.reason)
            return

        project = next((p for p in self.catalog.projects if p.name == q["project"]), None)
        if project is None:
            return

        # THE ASK DID NOT FREEZE ANYTHING
        latest = pstore.latest_validation_round(wo_id=wo["id"])
        still = autoreview.decide(
            numbered, wo, project.validation,
            round_outcome=str((latest or {}).get("outcome") or ""),
            # The round travels with its outcome here too: this is the site that writes the hold on the give-up...
            round_n=int((latest or {}).get("round") or 0),
            round_reason=str((latest or {}).get("reason") or ""),
            refusal_answered=ops.refusal_answered(pstore, wo["id"]),
            undeclared_delivery=invariants_mod.undeclared_delivery(pstore, wo),
            # On a CONFIRMATION the question being delivered is not the one condition 6 would trip on:...
            asked_question_id=int((assumption.get("neo_question_id") or q["id"])
                                  if confirming else q["id"]),
            # CONDITION 7 IS RE-RUN THE WAY THE ASK RAN IT
            stakes=self._settle_stakes(project, wo, numbered))
        if not still.armed:
            # A RULING DROPPED FOR A TRANSIENT REASON IS ASKED AGAIN, on either pass (#833)
            retry = (still.code in autoreview.TRANSIENT_DROPS
                     and str(wo.get("status") or "") not in TERMINAL_STATUSES)
            if not retry:
                # Escalated rather than left `answered`: the assumption is the user's again
                neo_store.mark(q["id"], "escalated", reason=still.reason,
                               cause=autoreview.escalation_cause(
                                   ruling, escalate=bool(verdict.get("escalate"))))
            self._note_autoreview_held(pstore, wo["id"], still, settling=True)
            pstore.add_event(wo["id"], "autoreview_escalated", {
                "assumption_id": assumption["id"], "n": numbered.get("n"),
                "reason": still.reason, "stakes": ruling.stakes,
                "model": ruling.model, "neo_question_id": q["id"],
                "overridden": False, "dropped": still.code, "re_asked": retry})
            if retry:
                # §4.3, IN THIS ORDER and after the record of the drop is complete
                fresh = ("confirmation will be asked against the delivered result"
                         if confirming else "ruling will be asked once it waits on a review")
                neo_store.supersede(
                    q["id"], f"SUPERSEDED — the OS dropped its own ruling: "
                             f"{still.reason}. A fresh {fresh}.", still.reason)
                if confirming:
                    pstore.clear_assumption_confirmation(assumption["id"])
                else:
                    pstore.clear_assumption_question(assumption["id"])
            log.info("auto-review dropped its ruling on assumption #%s of %s: %s",
                     numbered.get("n"), wo["id"], still.reason)
            return
        try:
            out = ops.accept_assumption(
                pstore, project.path, wo, numbered, reason=ruling.reason,
                model=ruling.model, question_id=int(q["id"]),
                cfg=project.validation)
        except Exception:  # noqa: BLE001 — one ruling must never kill the drain
            log.exception("accepting assumption %s of %s failed", assumption["id"],
                          wo["id"])
            return
        if confirming:
            # BOTH events on one assumption is the designed state
            pstore.add_event(wo["id"], "autoreview_confirmed", {
                "assumption_id": assumption["id"], "n": numbered.get("n"),
                "reason": ruling.reason, "stakes": ruling.stakes,
                "model": ruling.model, "neo_question_id": q["id"], **prior})
        log.info("auto-review accepted assumption #%s of %s (%s left) — %s",
                 numbered.get("n"), wo["id"], out["pending"], out["status"])
        if not out["settled"]:
            return
        decided = [a for a in pstore.all_assumptions(wo["id"])
                   if a.get("decided_by") == autoreview.DECIDER]
        central.add_inbox(
            project=q["project"], level="info",
            title=f"The OS decided {len(decided)} assumption(s) for you on {wo['id']}",
            body=f"{wo.get('title') or ''}\n"
                 f"{wo['id']} is now {out['status']} — it is off your review list.\n"
                 f"What it accepted, and why: jarvis wo show {wo['id']}\n"
                 f"If any of it was wrong: jarvis neo review {q['id']} "
                 f"--correct \"…\" teaches Neo not to do it again.",
            wo_id=wo["id"])

    def _asked_early(self, pstore: ProjectStore, wo_id: str, question_id: int) -> bool:
        """Was this assumption question filed by the EARLY pass?"""
        from . import db

        for event in pstore.events_of_kind(wo_id, "autoreview_asked"):
            payload = db.from_json(event["payload"], {})
            if int(payload.get("neo_question_id") or 0) == question_id:
                return bool(payload.get("early"))
        return False

    def _record_early_assumption_verdict(self, neo_store: Any, pstore: ProjectStore,
                                         wo: dict, assumption: dict, q: dict,
                                         verdict: dict) -> None:
        """Neo's ruling on an assumption it was asked about mid-run"""
        from . import autoreview, ops

        ruling = autoreview.read_ruling(verdict,
                                        default_model=self.catalog.os.neo.model)
        objected = (not ruling.accept and not ruling.overridden
                    and not verdict.get("escalate")
                    and ruling.stakes in autoreview.ROUTINE_STAKES)
        if not (ruling.accept or objected):
            if not verdict.get("escalate"):
                # `_deliver_assumption_verdict`'s branch
                neo_store.mark(q["id"], "escalated", reason=ruling.reason,
                               cause=autoreview.escalation_cause(
                                   ruling, escalate=bool(verdict.get("escalate"))))
            pstore.add_event(wo["id"], "autoreview_escalated", {
                "assumption_id": assumption["id"], "n": assumption.get("n"),
                "reason": ruling.reason, "stakes": ruling.stakes,
                "model": ruling.model, "neo_question_id": q["id"],
                "overridden": ruling.overridden, "early": True})
            log.info("early review left assumption #%s of %s with the user: %s",
                     assumption.get("n"), wo["id"], ruling.reason)
            return
        ops.record_provisional_verdict(
            pstore, wo, assumption,
            verdict="accept" if ruling.accept else "object",
            reason=ruling.reason, model=ruling.model, stakes=ruling.stakes,
            question_id=int(q["id"]))
        log.info("early review judged assumption #%s of %s provisionally %s — %s",
                 assumption.get("n"), wo["id"],
                 "accept" if ruling.accept else "object", ruling.reason)

    def _carry_catch_up(self, project: ProjectSpec, store: ProjectStore, wo: dict,
                        pr: Any, *, judged: str, head: str,
                        round_n: int) -> CarryOutcome:
        """Prove this head added no unjudged commit, and carry the verdict onto it (docs/superpowers/specs/2026-09-27-a-catch-up-with-main-costs-no-round.md §3.4.)"""
        from . import branchproof, ops

        base = str(getattr(pr, "base_ref", "") or "")
        pr_url = str(wo.get("pr_url") or "")
        if not judged or not head or head == judged or not base or not pr_url:
            return CarryOutcome(False, False)

        def refuse(proof: str, detail: str,
                   chain: tuple[str, ...] = ()) -> CarryOutcome:
            if ops.record_carry_refusal(store, wo, judged=judged, head_sha=head,
                                        proof=proof, detail=detail, chain=chain):
                log.info("[%s] %s: not carrying round %s onto %s — %s", project.name,
                         wo["id"], round_n, head[:10], detail)
            return CarryOutcome(False, False)

        pull_ref = f"refs/pull/{pr_url.rsplit('/', 1)[-1]}/head"
        if not branchproof.fetch(project.path, base, pull_ref):
            return refuse(ops.PROOF_FETCH,
                          f"`git fetch origin {base} {pull_ref}` failed, so the pull "
                          f"request's own diff cannot be compared locally")
        before = branchproof.diff_fingerprint(project.path, base, judged)
        after = branchproof.diff_fingerprint(project.path, base, head)
        if not before or not after:
            return refuse(ops.PROOF_FETCH,
                          "the diff this branch adds on top of its merge base could not "
                          "be computed locally")
        if before != after:
            # THE REFUSAL IS UNCHANGED, `(head
            refuse(ops.PROOF_PATCH_ID,
                   f"the diff on {head[:10]} is not the diff round "
                   f"{round_n} read, so the merge resolved content nobody judged")
            if not ops.rebind_possible(store, wo, head=head, project=project.name,
                                       cfg=project.validation):
                return CarryOutcome(False, False)
            # #1141: merges alone, in ANY topology, earn the rebind
            if branchproof.merges_only(project.path, judged, head, base):
                return CarryOutcome(False, True)
            def unreadable(exc: Exception) -> CarryOutcome:
                log.info("[%s] %s: GitHub would not say what %s merged (%s)",
                         project.name, wo["id"], head[:10], exc)
                return CarryOutcome(False, False)

            chain, failed = self._merge_chain(project, wo, pr_url=pr_url, judged=judged,
                                              head=head, base=base,
                                              github_error=unreadable)
            if failed is not None:
                return failed
            return CarryOutcome(False, bool(chain))
        def merges_rebind(refused: CarryOutcome) -> CarryOutcome:
            # #1141: no carry, but merges alone still cost no counted round
            rebind = (branchproof.merges_only(project.path, judged, head, base)
                      and ops.rebind_possible(store, wo, head=head, project=project.name,
                                              cfg=project.validation))
            return CarryOutcome(False, True) if rebind else refused

        chain, failed = self._merge_chain(
            project, wo, pr_url=pr_url, judged=judged, head=head, base=base,
            github_error=lambda exc: refuse(
                ops.PROOF_READ,
                f"GitHub would not say what {head[:10]} merged ({exc})"))
        if failed is not None:
            return merges_rebind(failed)
        if not chain:
            return merges_rebind(refuse(
                ops.PROOF_CHAIN, f"{head[:10]} is not {judged[:10]} plus merges of "
                f"{base} or of commits {judged[:10]} already contained"))
        # The NEWEST BASE commit walked, and `""` when none was: past Neo question 806 a walked merge may...
        bases = [merged for _sha, merged, is_base in chain if is_base]
        carried = ops.carry_merge_chain(
            store, wo, judged=judged, head=head, chain=chain, base=base,
            base_sha=bases[-1] if bases else "", fingerprints=(before, after))
        if carried is None:
            return refuse(ops.PROOF_CHAIN,
                          "the round this would have been carried from is no longer the "
                          "verdict on the judged commit")
        log.info("[%s] %s: round %s passed on %s and now covers %s — %d merge(s), %d of "
                 "%s, no round spent", project.name, wo["id"], carried["round"],
                 judged[:10], head[:10], len(chain), len(bases), base)
        return CarryOutcome(True, False)

    @staticmethod
    def _merge_chain(project: ProjectSpec, wo: dict, *, pr_url: str, judged: str,
                     head: str, base: str,
                     github_error: Callable[[Exception], CarryOutcome],
                     ) -> tuple[tuple[tuple[str, str, bool], ...] | None,
                                CarryOutcome | None]:
        """Walk `ci.base_merge_chain`, or say why it could not be walked"""
        from . import ci, github

        try:
            return ci.base_merge_chain(pr_url, judged, head, base_ref=base,
                                       cwd=project.path), None
        except github.GitHubError as exc:
            return None, github_error(exc)
        except Exception:  # noqa: BLE001 — a parked pull request stays parked
            log.exception("[%s] could not read the chain behind %s", project.name,
                          wo["id"])
            return None, CarryOutcome(False, False)

    def _catch_up_with_base(self, project: ProjectSpec, store: ProjectStore, wo: dict,
                            pr: Any) -> Any:
        """Merge the base into this branch, when it is behind (docs/superpowers/specs/2026-09-27-a-catch-up-with-main-costs-no-round.md §5.)"""
        from . import branchproof, ci, github, ops

        wo_id = wo["id"]
        base = str(getattr(pr, "base_ref", "") or "")
        pr_url = str(wo.get("pr_url") or "")
        if not base or not pr_url:
            return ops.CatchUp(pr, ops.CATCH_UP_FAILED,
                               "the work order records no pull request or no base branch")
        if (worker_session.busy(store, wo_id) or store.queued_messages(wo_id)
                or store.validation_round_open(wo_id)):
            log.debug("[%s] %s is in flight — catching up with %s deferred",
                      project.name, wo_id, base)
            return ops.CatchUp(pr, ops.CATCH_UP_DEFERRED,
                               "the turn, message or validation round in flight",
                               cause=ops.CATCH_UP_DEFER_IN_FLIGHT)
        pull_ref = f"refs/pull/{pr_url.rsplit('/', 1)[-1]}/head"
        if not branchproof.fetch(project.path, base, pull_ref):
            # logged there, with the refs it asked for
            return ops.CatchUp(pr, ops.CATCH_UP_FAILED,
                               f"the checkout could not fetch `{base}`")
        base_oid = branchproof.tip(project.path, f"origin/{base}")
        # Every return from here down carries the FETCHED tip
        if not ops.catch_up_needed(pr, repo=project.path, base_tip=base_oid):
            return ops.CatchUp(pr, ops.CATCH_UP_NOT_NEEDED, "", base_tip=base_oid)
        if base_oid and ops.base_heal_spent(store, wo_id, base_oid):
            return ops.CatchUp(pr, ops.CATCH_UP_EXHAUSTED,
                               f"this branch has already been rebuilt against "
                               f"{base_oid[:10]} once", base_tip=base_oid)
        policy = ops.catch_up_policy(store)
        attempts = ops.catch_up_attempts(store, wo_id, policy=policy)
        cap = policy.catch_up_max
        if attempts >= cap:
            window = _minutes_label(policy.catch_up_window_minutes)
            log.debug("[%s] %s has been caught up with %s %d times in %s — leaving it "
                      "held", project.name, wo_id, base, cap, window)
            return ops.CatchUp(pr, ops.CATCH_UP_EXHAUSTED,
                               f"it has already been caught up {cap} time(s)"
                               f" in the last {window}, which is the cap "
                               f"({cap}) — the OS chases it again once that "
                               f"window clears",
                               base_tip=base_oid)

        # GUARD 6, and it is `heal_inherited_failure`'s review-round-1 note: `pr` was read at the top of the...
        try:
            fresh = github.pr_view(pr_url, cwd=project.path)
        except github.GitHubError as e:
            log.debug("[%s] could not re-read %s before catching it up: %s",
                      project.name, pr_url, e)
            return ops.CatchUp(pr, ops.CATCH_UP_FAILED,
                               f"the OS could not re-read the pull request: {e.reason}",
                               base_tip=base_oid)
        judged = ProjectStore.validated_head(
            store.latest_validation_round(wo_id=wo_id)) or ""
        head_before = fresh.head_oid
        if not head_before or head_before != judged:
            return ops.CatchUp(fresh, ops.CATCH_UP_DEFERRED,
                               "the push that moved the head off the judged commit",
                               base_tip=base_oid,
                               cause=ops.CATCH_UP_DEFER_HEAD_MOVED)
        # EXPONENTIAL BACKOFF, after the head check so the OS's own merge commit is still carried (#1188)
        if ops.catch_up_backoff(store, wo_id, policy=policy) > 0:
            delay = ops.catch_up_delay(attempts, policy)
            log.debug("[%s] %s was caught up %d time(s) — backing off %ds before the next",
                      project.name, wo_id, attempts, delay)
            # Worded from `attempts` and `delay` only, so the hold is recorded once per wait
            return ops.CatchUp(fresh, ops.CATCH_UP_DEFERRED,
                               f"the {_seconds_label(delay)} backoff after catch-up "
                               f"{attempts} of {cap}",
                               base_tip=base_oid, cause=ops.CATCH_UP_DEFER_BACKOFF)
        try:
            ci.update_branch(pr_url, cwd=project.path)
        except github.GitHubError as e:
            log.info("[%s] could not catch %s up with %s: %s", project.name, pr_url,
                     base, e)
            ops.record_base_update_failed(store, wo, base=base, base_sha=base_oid,
                                          reason=e.reason,
                                          cause=ops.BASE_UPDATE_BEHIND)
            return ops.CatchUp(fresh, ops.CATCH_UP_FAILED, e.reason, base_tip=base_oid)
        try:
            fresh = github.pr_view(pr_url, cwd=project.path)
        except github.GitHubError as e:
            # The update LANDED; only the read back failed
            log.info("[%s] caught %s up but could not re-read it: %s", project.name,
                     pr_url, e)
        ops.record_base_update(store, wo, base=base, base_sha=base_oid,
                               head_before=head_before, head_after=fresh.head_oid,
                               checks=fresh.failing, cause=ops.BASE_UPDATE_BEHIND)
        if fresh.head_oid == head_before:
            # #1188: GitHub builds the merge commit asynchronously, so this read can lag it
            log.info("[%s] %s was behind %s (%s) — caught it up; GitHub has not "
                     "published the merge commit yet and %s is still the head",
                     project.name, pr_url, base, base_oid[:10], head_before[:10])
        else:
            log.info("[%s] %s was behind %s (%s) — caught it up, %s is now %s",
                     project.name, pr_url, base, base_oid[:10], head_before[:10],
                     fresh.head_oid[:10])
        return ops.CatchUp(fresh, ops.CATCH_UP_DONE, "", base_tip=base_oid)

    def _rejudge_moved_head(self, project: ProjectSpec, store: ProjectStore,
                            wo: dict, decision: Any, *, rebind: bool = False) -> None:
        """Re-open a round on a head the OS's own repair loop moved (docs/superpowers/specs/2026-09-19-a-moved-head-re-judges-itself.md)"""
        from . import ops

        try:
            out = ops.rejudge_moved_head(store, project.path, wo, project=project.name,
                                         cfg=project.validation, decision=decision,
                                         rebind=rebind)
        except Exception:  # noqa: BLE001 — a parked pull request stays parked
            log.exception("[%s] could not re-judge %s", project.name, wo["id"])
            return
        if out is None:
            return
        if out["declined"]:
            # Two causes, two sentences: `next_round` is on the budget decline only (spec...
            if out.get("cause") == ops.REBIND_EXHAUSTED:
                log.info("[%s] %s: the head moved to %s and the OS has re-judged this "
                         "merge %s time(s) — leaving it for the user", project.name,
                         wo["id"], out["head_sha"][:10], out["rebinds"])
            elif not out["judged_sha"]:
                # Same false claim as the success log below
                log.info("[%s] %s: the passed round recorded no commit and round %s "
                         "would be the last — leaving it for the user", project.name,
                         wo["id"], out["next_round"])
            else:
                log.info("[%s] %s: the head moved to %s and round %s would be the "
                         "last — leaving it for the user", project.name, wo["id"],
                         out["head_sha"][:10], out["next_round"])
        elif not out["judged_sha"]:
            # NOTHING WAS JUDGED, so "the head moved from to bbbb1111bb" is the same false claim the reason text...
            log.info("[%s] %s: the passed round recorded no commit — re-judging the "
                     "pull request at %s as round %s", project.name, wo["id"],
                     out["head_sha"][:10], out["round"])
        else:
            log.info("[%s] %s: the head moved from %s to %s — re-judging it as round %s",
                     project.name, wo["id"], out["judged_sha"][:10],
                     out["head_sha"][:10], out["round"])

    def _protection_fact(self, project: ProjectSpec, pr: Any) -> str:
        """What the merge request may say about branch protection (docs/superpowers/specs/2026-09-24-an-auto-merge-request-that-proves-itself.md)"""
        from . import automerge, github

        owner_repo = github.origin_repo(project.path)
        if owner_repo is None or not pr.base_ref:
            return ""
        try:
            protection = github.branch_protection(owner_repo, pr.base_ref,
                                                  cwd=project.path)
        except github.GitHubError as exc:
            log.info("[%s] branch protection on %s unreadable: %s", project.name,
                     pr.base_ref, exc)
            protection = automerge.PROTECTION_UNREADABLE
        return automerge.protection_fact(protection, pr.base_ref)

    def _supersede_stale_request(self, store: ProjectStore, approval: dict,
                                 pr: Any, *, base_tip: str = "") -> None:
        """Close a pending merge request whose base has moved"""
        from .neo_store import NeoStore

        base = str(getattr(pr, "base_ref", "") or "the base")
        moved = f" to {base_tip[:10]}" if base_tip else ""
        reason = (f"`{base}` has moved{moved} "
                  f"since this request was filed against "
                  f"{str(approval['base_oid'] or '')[:10] or 'an unrecorded base'}; the "
                  f"CI evidence in it no longer describes what this merge would land on. "
                  f"Nothing is authorised and nothing is refused — the OS catches the "
                  f"branch up and files a fresh request on the new commit.")
        store.supersede_approval(approval["id"], reason)
        if approval["neo_question_id"]:
            neo_store = NeoStore()
            try:
                neo_store.supersede(approval["neo_question_id"],
                                    f"SUPERSEDED — `{base}` moved", reason)
            finally:
                neo_store.close()

    def _drift_hold(self, store: ProjectStore, wo: dict) -> Any:
        """The open scope-drift finding this merge must not land over, or None (#1063)"""
        from . import automerge
        from .neo_store import NeoStore

        alarm = automerge.open_drift_alarm(store.alarms_of(wo["id"]))
        if alarm is None:
            return None
        question = None
        # Only `escalated` has a question in flight; every other status reads off the row
        if alarm["status"] == "escalated" and alarm["neo_question_id"]:
            neo = NeoStore()
            try:
                question = neo.get(int(alarm["neo_question_id"]))
            finally:
                neo.close()
        # A wedged one raises the flag through `invariants.true_blockers`, not from here
        return automerge.drift_hold(alarm, question)

    def _note_automerge_held(self, store: ProjectStore, wo_id: str,
                             decision: Any) -> None:
        """Record ONCE, per (commit, reason), that the OS declined to merge (#263)"""
        from . import automerge

        if decision.code in (automerge.HELD_DISABLED,   # both unreachable via the poll
                             automerge.HELD_STATUS):    # `auto_merge` returns before here
            return
        key = (str(decision.head_sha or ""), str(decision.code or ""),
               str(decision.reason or ""), int(decision.round_n or 0))
        if not _hold_is_news(store, wo_id, "automerge_held", key,
                             lambda p: (str(p.get("head_sha") or ""),
                                        str(p.get("code") or ""),
                                        str(p.get("reason") or ""),
                                        int(p.get("round") or 0))):
            return
        # A LESSER READING OF THE SAME HEAD DOES NOT OVERWRITE THE ASK THE USER OWES (#1173)
        exhausted = invariants_mod.catch_up_exhausted_hold(store, wo_id)
        if (exhausted is not None and decision.code != automerge.HELD_BASE_MOVED
                and str(exhausted.get("head_sha") or "")
                == str(decision.head_sha or "")):
            return
        # THE PAYLOAD'S SHAPE HAS ONE DEFINITION, `automerge.hold_payload`
        store.add_event(wo_id, "automerge_held", automerge.hold_payload(decision))

    def _warn_automerge_failed(self, project: ProjectSpec, store: ProjectStore,
                               wo: dict, decision: Any, reason: str) -> None:
        """One inbox row for the FIRST failed merge of a commit"""
        from . import automerge

        if automerge.attempts(store, wo["id"], decision.judged_sha) != 1:
            return          # the caller records the event first, so 1 == this one
        self.central.add_inbox(
            project=project.name, level="warning",
            title=f"The OS could not merge the pull request for {wo['id']}",
            body=f"{wo.get('pr_url')}\n"
                 f"The panel passed it and the merge was authorised, but GitHub refused "
                 f"it on commit {decision.judged_sha[:10]}: {reason}\n"
                 f"Nothing was merged and nothing is stuck — merging it yourself works "
                 f"exactly as it always has.",
            wo_id=wo["id"])

    def _note_recompute_stalled(self, store: ProjectStore, wo: dict,
                                pr: Any) -> bool:
        """Say GitHub has stopped recomputing this pull request, ONCE per episode"""
        started = max(float(wo.get("pr_mergeable_unknown_since") or 0.0),
                      float(wo.get("pr_head_seen_at") or 0.0))
        told = store.events_of_kind(wo["id"], PR_RECOMPUTE_STALLED_EVENT)
        if told and float(told[-1]["ts"]) >= started:
            return False
        store.add_event(wo["id"], PR_RECOMPUTE_STALLED_EVENT,
                        {"pr_url": wo.get("pr_url"),
                         "head_oid": str(wo.get("pr_head_oid") or ""),
                         "ref_oid": str(wo.get("pr_ref_oid") or ""),
                         "branch": getattr(pr, "head_ref", ""),
                         "mergeable": getattr(pr, "mergeable", None) or "UNKNOWN",
                         # WHICH OF THE TWO CONDITIONS THIS IS: with no tip observed there is nothing to compare
                         "unknown_since": wo.get("pr_mergeable_unknown_since")})
        return True

    def resync_pull_request(self, project: ProjectSpec, store: ProjectStore,
                            wo: dict, pr: Any) -> bool:
        """Close and reopen this pull request ONCE, to make GitHub recompute it (#995)"""
        from . import github, pr_resync

        url = wo.get("pr_url")
        stuck = {"pr_url": url,
                 "head_oid": str(wo.get("pr_head_oid") or ""),
                 "ref_oid": str(wo.get("pr_ref_oid") or ""),
                 "branch": getattr(pr, "head_ref", "")}
        store.add_event(wo["id"], PR_RESYNC_STARTED_EVENT, dict(stuck))
        for stage, call in (("close", pr_resync.pr_close),
                            ("reopen", pr_resync.pr_reopen)):
            try:
                call(url, cwd=project.path)
            except Exception as e:  # noqa: BLE001 — every failure is the same decision
                log.warning("[%s] could not %s %s to resync %s: %s", project.name,
                            stage, url, wo["id"], e)
                store.add_event(wo["id"], PR_RESYNC_FAILED_EVENT,
                                {**stuck, "stage": stage, "error": str(e),
                                 "reason": getattr(e, "reason",
                                                   github.GitHubError.REFUSED)})
                if stage == "reopen":
                    # See the docstring: the OS owns a shut pull request and no other reader on this base would ever say...
                    store.flag_attention(
                        wo["id"],
                        f"could not reopen {url} after closing it to make GitHub "
                        f"recompute it: the pull request is shut and its head is stuck "
                        f"at {stuck['head_oid'][:7] or '?'}"[:300])
                return False
        store.add_event(wo["id"], PR_RESYNC_DONE_EVENT, dict(stuck))
        log.info("[%s] closed and reopened %s to make GitHub recompute %s",
                 project.name, url, wo["id"])
        return True

    def rerun_aborted_checks(self, project: ProjectSpec, store: ProjectStore,
                             wo: dict, pr: Any) -> None:
        """Re-run a check GitHub cancelled, ONCE per head, then hand it to the user (#1123)"""
        from . import ci_rerun, github

        wo_id = wo["id"]
        head = str(pr.head_oid or "")
        waiting = bool(pr.state == "OPEN" and head
                       and wo["status"] in ci_rerun.CI_WAIT_STATUSES
                       and str(pr.merge_state or "").upper() == "BLOCKED"
                       and not pr.failing)
        running = [c for c in pr.checks
                   if c["status"].upper() in github.UNFINISHED_STATUSES - {"EXPECTED"}]
        aborted = pr.aborted if waiting and not running else ()
        stuck = str(wo.get("pr_checks_stuck_head") or "")
        if stuck:
            cause = str((ci_rerun.episode(store, wo_id, stuck).stuck or {}).get("cause")
                        or "")
            if (stuck == head and waiting and not running
                    and (aborted or cause == ci_rerun.CAUSE_NEVER_RAN)):
                return      # still stuck, and already said so
            store.update_work_order(wo_id, pr_checks_stuck_head=None)
            store.add_event(wo_id, ci_rerun.RECOVERED_EVENT,
                            {"head_sha": stuck, "now_head": head,
                             "pr_url": wo.get("pr_url")})
        if not waiting or running:
            return
        if aborted:
            ep = ci_rerun.episode(store, wo_id, head)
            if ep.stuck is not None:
                return
            checks = ", ".join(f"{c['name'] or '(unnamed check)'}: {c['conclusion']}"
                               for c in aborted)
            update = ci_rerun.update_branch_command(str(wo.get("pr_url") or ""))
            if ep.reruns:
                ids = [int(i) for i in ep.reruns[-1].get("run_ids") or []]
                self._ci_rerun_gave_up(store, wo, head, ci_rerun.CAUSE_CANCELLED_AGAIN,
                                       checks, (*map(ci_rerun.rerun_command, ids),
                                                update))
                return
            try:
                runs = ci_rerun.head_runs(head, cwd=project.path)
            except github.GitHubError as e:
                # Unread is not unrecoverable: no attempt is spent, next tick asks again
                log.info("[%s] could not list the runs of %s for %s: %s", project.name,
                         head[:10], wo_id, e)
                return
            targets = ci_rerun.runs_to_rerun(aborted, runs)
            if not targets:
                self._ci_rerun_gave_up(store, wo, head, ci_rerun.CAUSE_NO_RUN, checks,
                                       (update,))
                return
            for run in targets:
                try:
                    ci_rerun.rerun(run.run_id, cwd=project.path)
                except github.GitHubError as e:
                    self._ci_rerun_gave_up(store, wo, head, ci_rerun.CAUSE_REFUSED,
                                           checks, (ci_rerun.rerun_command(run.run_id),
                                                    update), error=str(e)[:300])
                    return
            store.add_event(wo_id, ci_rerun.RERUN_EVENT, {
                "head_sha": head, "pr_url": wo.get("pr_url"), "checks": checks,
                "run_ids": [r.run_id for r in targets],
                "workflows": [r.workflow for r in targets]})
            log.info("[%s] re-ran cancelled CI on %s for %s (runs %s)", project.name,
                     head[:10], wo_id, ", ".join(str(r.run_id) for r in targets))
            return
        # A REQUIRED CHECK THAT NEVER REPORTED: one protection read per head per process
        seen = self.__dict__.setdefault("_ci_required_read", set())
        if wo["status"] != "waiting_pr_merge" or (wo_id, head) in seen:
            return
        seen.add((wo_id, head))
        owner_repo = github.origin_repo(project.path)
        if owner_repo is None or not pr.base_ref:
            return
        try:
            protection = github.branch_protection(owner_repo, pr.base_ref,
                                                  cwd=project.path)
        except github.GitHubError as e:
            log.info("[%s] branch protection on %s unreadable: %s", project.name,
                     pr.base_ref, e)
            return
        reported = {c["name"] for c in pr.checks if c["status"].upper() != "EXPECTED"}
        missing = [n for n in (protection.required_checks if protection else ())
                   if n not in reported]
        if missing:
            self._ci_rerun_gave_up(
                store, wo, head, ci_rerun.CAUSE_NEVER_RAN, ", ".join(missing),
                (ci_rerun.update_branch_command(str(wo.get("pr_url") or "")),))

    def _ci_rerun_gave_up(self, store: ProjectStore, wo: dict, head: str, cause: str,
                          checks: str, commands: tuple[str, ...], **extra: Any) -> None:
        """Record the ONE give-up for this head; `invariants.true_blockers` raises it (#1123)"""
        from . import ci_rerun

        pr_url = str(wo.get("pr_url") or "")
        store.add_event(wo["id"], ci_rerun.STUCK_EVENT, {
            "head_sha": head, "pr_url": pr_url, "cause": cause, "checks": checks,
            "commands": list(commands),
            "blocker": ci_rerun.stuck_blocker(pr_url, head, cause, checks, commands),
            **extra})
        store.update_work_order(wo["id"], pr_checks_stuck_head=head)
        log.info("%s: cancelled CI on %s needs the user (%s)", wo["id"], head[:10],
                 cause)

    def _note_reopened(self, project: ProjectSpec, store: ProjectStore,
                       wo: dict) -> bool:
        """This open pull request was recorded as closed: say so, once"""
        if not store.pr_closure_told(wo["id"]):
            return False
        store.add_event(wo["id"], "pr_reopened", {"pr_url": wo.get("pr_url"),
                                                  "status": wo["status"]})
        store.update_work_order(wo["id"], pr_state=None)
        if wo["attention_reason"] == invariants_mod.PR_CLOSED_BLOCKER:
            # RELABELLED, not cleared
            fresh = store.get_work_order(wo["id"])
            blockers = invariants_mod.true_blockers(store, fresh)
            if blockers:
                store.flag_attention(wo["id"], blockers[0])
            else:
                store.clear_attention(wo["id"])
        return True

    def _note_resync_reopened(self, project: ProjectSpec, store: ProjectStore,
                              wo: dict, pr: Any) -> bool:
        """This pull request is open again after the OS's resync window: record it, once"""
        if not pr_resync_unreopened(store, wo):
            return False
        store.add_event(wo["id"], PR_RESYNC_DONE_EVENT,
                        {"pr_url": wo.get("pr_url"), "head_oid": pr.head_oid or "",
                         "observed": True})
        log.info("[%s] %s is open again after the OS closed it to resync it — the "
                 "window on %s is over", project.name, wo.get("pr_url"), wo["id"])
        return True

    def heal_inherited_failure(self, project: ProjectSpec, store: ProjectStore,
                               wo: dict, pr: Any,
                               base_ci: dict[str, Any]) -> tuple[bool, Any]:
        """A pull request red only because its base was (docs/superpowers/specs/2026-09-18-a-red-base-heals-itself.md)"""
        from . import ci, github, ops

        base_ref = pr.base_ref or ""
        # The workflows THIS pull request is failing, de-duplicated and in order
        workflows = tuple(dict.fromkeys(
            c.get("workflow") or "" for c in pr.red if c.get("workflow")))
        if not base_ref or not workflows:
            return False, pr

        if base_ref not in base_ci:
            try:
                base_ci[base_ref] = ci.base_runs(base_ref, cwd=project.path)
            except github.GitHubError as e:
                # Cached as None so one unreadable base costs one call per tick rather than one per parked pull...
                log.debug("[%s] could not read %s's CI: %s", project.name, base_ref, e)
                base_ci[base_ref] = None
        runs = base_ci[base_ref]
        if runs is None:
            return False, pr

        if ci.base_is_red(runs, workflows):
            if ops.record_base_health(store, wo, red=True, base=base_ref):
                log.info("[%s] %s is red — holding %s rather than nudging it",
                         project.name, base_ref, wo["id"])
            return True, pr
        ops.record_base_health(store, wo, red=False, base=base_ref)

        if not any(ci.inherited(c, runs) for c in pr.red):
            return False, pr
        base_sha = ci.base_sha(runs, workflows)
        if not base_sha or ops.base_heal_spent(store, wo["id"], base_sha):
            # Already rebuilt on this base and still red: the failure is the branch's own
            return False, pr
        if worker_session.busy(store, wo["id"]) or store.validation_round_open(wo["id"]):
            log.debug("[%s] %s is in flight — base heal deferred", project.name,
                      wo["id"])
            return True, pr

        # RE-READ THE HEAD HERE, NOT AT THE TOP OF THE POLL (review round 1)
        try:
            pr = github.pr_view(wo["pr_url"], cwd=project.path)
        except github.GitHubError as e:
            log.debug("[%s] could not re-read %s before updating it: %s", project.name,
                      wo["pr_url"], e)
            return False, pr
        head_before = pr.head_oid
        # `carry_validated_head`'s fact 2: the verdict may only be carried if the commit the panel accepted...
        judged = ProjectStore.validated_head(
            store.latest_validation_round(wo_id=wo["id"])) or ""
        try:
            ci.update_branch(wo["pr_url"], cwd=project.path)
        except github.GitHubError as e:
            log.info("[%s] could not rebuild %s against %s: %s", project.name,
                     wo["pr_url"], base_ref, e)
            ops.record_base_update_failed(store, wo, base=base_ref, base_sha=base_sha,
                                          reason=e.reason,
                                          cause=ops.BASE_UPDATE_BASE_RED)
            return False, pr
        try:
            pr = github.pr_view(wo["pr_url"], cwd=project.path)
        except github.GitHubError as e:
            # The update LANDED; only the read back failed
            log.info("[%s] rebuilt %s but could not re-read it: %s", project.name,
                     wo["pr_url"], e)
        ops.record_base_update(store, wo, base=base_ref, base_sha=base_sha,
                               head_before=head_before, head_after=pr.head_oid,
                               checks=pr.failing, cause=ops.BASE_UPDATE_BASE_RED)
        log.info("[%s] %s was red only because %s was — rebuilt it against %s",
                 project.name, wo["pr_url"], base_ref, base_sha[:10])
        if judged and judged == head_before and pr.head_oid != head_before:
            # FACT 3, AND IT IS THE ONE THAT CANNOT BE RACED
            try:
                parents = ci.commit_parents(wo["pr_url"], pr.head_oid,
                                            cwd=project.path)
            except github.GitHubError as e:
                log.info("[%s] cannot prove what %s merged, not carrying the verdict: "
                         "%s", project.name, pr.head_oid[:10], e)
                parents = ()
            carried = ops.carry_validated_head(store, wo, judged=judged,
                                               head_after=pr.head_oid, base=base_ref,
                                               base_sha=base_sha, parents=parents)
            if carried:
                log.info("[%s] carried round %s's verdict from %s to %s on %s",
                         project.name, carried["round"], judged[:10],
                         carried["carried_head_sha"][:10], wo["id"])
        return True, pr

    def heal_pull_request(self, project: ProjectSpec, store: ProjectStore, wo: dict,
                          repair: Any, what: str, **fields: Any) -> None:
        """Something the OS can ask this worker to fix itself: conflicts (docs/superpowers/specs/2026-09-19-an-attempt-the-worker-could-not-make.md)"""
        from . import ops

        if not wo.get("session_id"):
            return
        if store.queued_messages(wo["id"]) or worker_session.busy(store, wo["id"]):
            return
        # A GATE UNDER REVIEW REFUSES THE REPAIR BEFORE IT STARTS (issue #469)
        blocking = store.pending_approvals(wo["id"])
        if blocking:
            if ops.defer_pr_repair(store, wo, repair, blocking[0]):
                log.info("[%s] %s is %s but gate %s is under review — repair deferred",
                         project.name, wo["id"], what, blocking[0]["id"])
            return
        # THE ROUND MACHINE OWNS THIS SESSION
        if store.validation_round_open(wo["id"]):
            log.debug("[%s] %s has a validation round open — repair deferred",
                      project.name, wo["id"])
            return
        # ...and the other half of #469: a budget ALREADY spent that way
        refunded = ops.rearm_pr_repair(store, wo, repair)
        if refunded:
            log.info("[%s] %s: %s attempts were spent behind a gate the worker could "
                     "not pass — budget re-armed", project.name, wo["id"], refunded)
        out = ops.nudge_pr_repair(store, wo, repair, **fields)
        if out["gave_up"]:
            log.info("[%s] %s still %s after %s attempts — %s needs the user",
                     project.name, wo["pr_url"], what, out["attempts"], wo["id"])
        else:
            log.info("[%s] %s %s — asking %s to fix it (attempt %s)",
                     project.name, wo["pr_url"], what, wo["id"], out["attempts"])

    def _deliver_triage_verdict(self, central: CentralStore, q: dict,
                                verdict: dict) -> None:
        """Route a filed bug now that Neo has ruled on the priority it was claimed at"""
        from . import issues

        try:
            out = issues.settle_triage(self.catalog, q, verdict)
        except Exception:  # noqa: BLE001 — one unreadable row must not stall the queue
            log.exception("triage verdict for neo question %s failed", q.get("id"))
            return
        log.info("[%s] triage %s: %s -> %s%s", q.get("project"), out.get("outcome"),
                 out.get("claimed"), out.get("settled"),
                 f" ({out['wo_id']})" if out.get("wo_id") else "")
        # The head of the reasoning, and a pointer to the rest
        why = (out.get("reason") or "").strip()
        head = (why[:300] + ("…" if len(why) > 300 else "")) or "no reason given"
        full = ("Neo's reasoning in full: "
                f"{notify.neo_question_url(self.catalog, q.get('id'))}")
        start = issues.START_COMMAND.format(url=out["issue_url"])
        if out["outcome"] == "unconfirmed":
            central.add_inbox(
                project=q.get("project") or "jarvis-os", level="warning",
                title=f"a `{out['claimed']}` bug report is UNCONFIRMED",
                body=(f"{out['issue_url']}\nNeo could not settle the priority "
                      f"({head}), so NOTHING was dispatched and no release was cut. The "
                      f"issue stands at `{out['claimed']}` as a claim; start work on it "
                      f"with `{start}` if you agree with the rating.\n{full}"))
        elif out["outcome"] == "downgraded":
            central.add_inbox(
                project=q.get("project") or "jarvis-os", level="info",
                title=f"Neo downgraded a `{out['claimed']}` bug to "
                      f"`{out['settled']}`",
                body=(f"{out['issue_url']}\n{head}\n"
                      f"Not dispatched. Start work on it with `{start}`.\n{full}"))

    def sync_issues(self, project: ProjectSpec, store: ProjectStore) -> None:
        """Keep the tracker saying what the OS is actually doing (#240)"""
        tracked = store.work_orders_tracking_issues()
        if not tracked:
            return
        from . import github, issues

        for wo in tracked:
            if not issues.needs_sync(store, wo):
                continue
            was = (wo.get("issue_state") or "")
            try:
                applied = issues.record_applied(store, wo, project.bugs.label)
            except github.GitHubError as e:
                # Same shape as the pull-request poll: one unreachable tracker must not hide the rest
                log.debug("[%s] could not sync %s for %s: %s", project.name,
                          wo.get("issue_url"), wo["id"], e)
                self._warn_issue_sync_broken(project, store, e)
                continue
            except Exception:  # noqa: BLE001 — never let one work order stall the rest
                log.exception("[%s] syncing the issue for %s failed", project.name,
                              wo["id"])
                continue
            log.info("[%s] %s is now %s (%s)", project.name, wo.get("issue_url"),
                     applied, wo["id"])
            # THE SHIP IS TRIGGERED BY THE TRACKER CLOSING, WHICH IS THE LANDING SIGNAL AND NOT `completed` (#232)
            from . import ops as ops_mod

            # TWO ROUTES TO A RELEASE, and expediting is the user's own: an expedited order carries an honest... (#961)
            if (applied == issues.CLOSED and was != issues.CLOSED
                    and ops_mod.routes_on_pull_request(store, wo)
                    and (issues.dispatches(wo.get("issue_priority") or "")
                         or issues.was_expedited(wo))):
                self.ensure_release(project, store, wo)

    #: How long a cached issue state is good for
    ISSUE_STATE_TTL = 3600

    #: How many issues ONE sweep will re-read
    ISSUE_REFRESH_PER_SWEEP = 10

    def sync_issue_references(self, project: ProjectSpec, store: ProjectStore) -> None:
        """Keep each linked issue saying how many work orders have pointed at it"""
        rows = store.issues_to_sync()
        if not rows:
            return
        from . import github, issues

        refreshed = 0
        for row in rows:
            url, repo = row["issue_url"], row["repo"]
            if not repo:
                continue
            try:
                checked = row["checked_at"]
                if ((checked is None or time.time() - checked > self.ISSUE_STATE_TTL)
                        and refreshed < self.ISSUE_REFRESH_PER_SWEEP):
                    issue = issues.view(url, repo)
                    refreshed += 1
                    store.record_issue(url, number=issue.number, repo=repo,
                                       title=issue.title, state=issue.state)
                    row = {**row, "state": issue.state}
                if (row.get("state") or "") != "OPEN":
                    continue
                for link in store.unannounced_links(url):
                    issues.comment(url, issues.reference_comment(
                        link["unit_id"], project.name, link["kind"]), repo)
                    store.mark_issue_announced(url, link["unit_id"])
                if int(row["refs"]) != int(row["refs_labelled"]):
                    issues.set_reference_label(url, int(row["refs"]), repo)
                    store.record_issue_label(url, int(row["refs"]))
            except github.GitHubError as e:
                log.debug("[%s] could not sync references on %s: %s", project.name,
                          url, e)
                self._warn_issue_sync_broken(project, store, e)
                continue
            except Exception:  # noqa: BLE001 — one issue must not stall the rest
                log.exception("[%s] syncing references on %s failed", project.name, url)
                continue

    #: The key under a work order's `metadata` that says "this order exists to ship fixes
    RELEASE_BATCH_KEY = release.BATCH_KEY

    #: What the release work order is told to do
    RELEASE_BRIEF = """\
Ship a release. These fixes have LANDED on `main` and the OS owes the fleet a release \
carrying them:

{fixes}

Run the ordinary release path and nothing else:

    scripts/shipit.sh --stage --wo {wo_id} --base <sha>

That is a PRIVILEGED ACTION and it will be gated — that is correct and expected. Make \
your case first (`jarvis gate request`), and read `jarvis brief gates` before you do. Do \
NOT invent a second release path, do not restart any service by hand, and do not drop \
`--stage`: an inline restart kills your own session mid-turn. Dropping or changing \
`--base` after approval invalidates the grant: the approved string names the commit.

Resolve `<sha>` BEFORE you ask, so the reviewer and the command name one commit: the \
newest commit on `origin/main` that carries every fix listed above and whose CI is \
green, written out in full (40 characters). If that base is red, or you cannot read \
CI's verdict on it, say so and stop — a release is not the place to fix a red build."""

    def ensure_release(self, project: ProjectSpec, store: ProjectStore,
                       wo: dict) -> str:
        """Make sure a release carrying this landed fix is on its way (#945)"""
        from . import db, issues
        from .project_store import OPEN_STATUSES

        if not self.self_dispatch:
            return ""  # releases only via the user's shipit unless os.autonomy is on
        if self._already_live(project, store, wo):
            return ""
        url = wo.get("issue_url") or ""
        # Expedited first: an order can be both
        reason = ("expedited by the filer" if issues.was_expedited(wo)
                  else f"confirmed `{wo.get('issue_priority')}` by Neo")
        line = (f"- {url} — fixed by `{wo['id']}`"
                + (f" ({wo['pr_url']})" if wo.get("pr_url") else "")
                + f" — {reason}")
        candidates: list[tuple[dict[str, Any], dict[str, Any], list]] = []
        for candidate in store.list_work_orders(statuses=OPEN_STATUSES,
                                                include_hidden=True):
            meta = db.from_json(candidate.get("metadata"), {}) or {}
            batch = meta.get(self.RELEASE_BATCH_KEY)
            if isinstance(batch, list):
                candidates.append((candidate, meta, batch))
        # §1.3, pass one over the one read: an url already batched anywhere is never added twice and never...
        for candidate, _meta, batch in candidates:
            if url in batch:
                return str(candidate["id"])
        # §1.1-1.2: `finished` is written by `jarvis wo finish`, which every delivery goes through
        for candidate, meta, batch in candidates:
            if store.events_of_kind(candidate["id"], "finished"):
                continue
            store.update_work_order(
                candidate["id"],
                metadata=db.to_json({**meta,
                                     self.RELEASE_BATCH_KEY: [*batch, url]}),
                description=f"{candidate.get('description') or ''}\n{line}")
            store.add_event(candidate["id"], "release_batched",
                            {"issue_url": url, "wo_id": wo["id"]})
            log.info("[%s] %s joins the pending release %s", project.name, url,
                     candidate["id"])
            return str(candidate["id"])

        # The brief names the work order's OWN id (`shipit.sh --wo`)
        decision = steward.gate(
            steward.Candidate("release", "major", project.name,
                              f"ship the fix for {url}", evidence=(url,),
                              parent_wo_id=str(wo["id"])),
            catalog=self.catalog, store=store,
            self_dispatch=self.self_dispatch)
        fresh = store.create_work_order(
            title=f"Ship the fix for {wo.get('title') or url}",
            description="", origin="jarvis",
            metadata=steward.stamped({self.RELEASE_BATCH_KEY: [url]}, decision))
        steward.linked(decision, fresh["id"])
        store.update_work_order(
            fresh["id"],
            description=self.RELEASE_BRIEF.format(wo_id=fresh["id"], fixes=line))
        store.add_event(fresh["id"], "release_batched",
                        {"issue_url": url, "wo_id": wo["id"]})
        log.info("[%s] %s landed — filed release %s", project.name, url, fresh["id"])
        return str(fresh["id"])

    #: Said on the FIXING order when no release was filed because a live tag already carries the fix... (#934)
    ALREADY_SHIPPED_EVENT = "release_already_shipped"

    def _already_live(self, project: ProjectSpec, store: ProjectStore,
                      wo: dict) -> bool:
        """Is this landed fix already in production — so there is nothing left to ship (#961)"""
        from . import release

        # `overtaken_by` reads `jarvis-*` tags and the OS's own production checkout
        if project.name != self._os_project():
            return False
        sha = self._merge_commit_of(project, store, wo)
        if not sha:
            return False
        found = release.overtaken_by(project.path, [sha])
        if found.error or not (found.tag and found.live):
            return False
        store.add_event(str(wo["id"]), self.ALREADY_SHIPPED_EVENT, {
            "tag": found.tag, "shas": [sha], "deployed": found.deployed,
            "detail": (f"{found.tag} already carries this fix and production runs "
                       f"{found.deployed} — no release filed")})
        log.info("[%s] %s is already live in %s — no release filed", project.name,
                 wo["id"], found.deployed)
        return True

    #: How long a red base defers a release order's dispatch
    RED_HOLD_SECONDS = 300

    #: Said once per BROKEN COMMIT on a release order held back by a red base (2026-09-27 spec §5.2)
    RED_HOLD_EVENT = "release_held_red_base"

    #: How long `main` may stay red before a release order stops waiting and asks the user (#807)
    RED_PARK_AFTER_SECONDS = 6 * 3600

    def clear_settled_preconditions(self, project: ProjectSpec,
                                    store: ProjectStore) -> None:
        """Clear every hold the OS can derive: a settled `bl-` item, deployed code (docs/superpowers/specs/2026-10-22-a-deployed-precondition-clears-itself.md §2.2)"""
        from . import preconditions

        for wo in store.list_work_orders(statuses=("pending",), include_hidden=True):
            for row in store.open_preconditions(wo["id"]):
                ref = str(row["ref"])
                if preconditions.deployed_target(ref):
                    self.clear_deployed_precondition(project, store, wo, row)
                    continue
                if not ref.startswith("bl-"):
                    continue
                item = self.central.get_backlog(ref)
                if item is None or item["status"] not in SETTLED_BACKLOG_STATUSES:
                    continue
                store.clear_precondition(int(row["id"]), by="os")
                log.info("[%s] %s: precondition %s cleared (%s is %s)", project.name,
                         wo["id"], ref, ref, item["status"])

    def clear_deployed_precondition(self, project: ProjectSpec, store: ProjectStore,
                                    wo: dict[str, Any], row: dict[str, Any]) -> None:
        """A `deployed:` hold clears when a release carrying that commit IS what production runs (docs/superpowers/specs/2026-10-22-a-deployed-precondition-clears-itself.md §2.2)"""
        from . import preconditions, release

        # `overtaken_by` reads `jarvis-*` tags and the OS's own production checkout, as `_already_live` does
        if project.name != self._os_project():
            return
        ref = str(row["ref"])
        sha = self.deployed_hold_sha(project, store,
                                     preconditions.deployed_target(ref))
        if not sha:
            return
        found = release.overtaken_by(project.path, [sha])
        if found.error or not (found.tag and found.live):
            return
        store.clear_precondition(int(row["id"]), by="os")
        log.info("[%s] %s: precondition %s cleared (%s is in %s and production runs %s)",
                 project.name, wo["id"], ref, sha[:8], found.tag, found.deployed)

    def deployed_hold_sha(self, project: ProjectSpec, store: ProjectStore,
                          target: str) -> str:
        """The commit a `deployed:` hold waits for: a work order's merge commit, else the target"""
        if not target.startswith("wo-"):
            return target
        try:
            dep = store.get_work_order(target)
        except KeyError:
            return ""
        return self._merge_commit_of(project, store, dep)

    def hold_red_release(self, project: ProjectSpec, store: ProjectStore) -> None:
        """Keep a pending release order out of dispatch while the base branch is red (docs/superpowers/specs/2026-09-27-an-expedited-order-that-lands-ships-a-release.md §5)"""
        from . import ci, evidence, github, ops

        # Same scope as `settle_shipped_releases`: the release path is the OS's own release script and the...
        if project.name != self._os_project():
            return
        due: list[dict[str, Any]] = []
        for candidate in store.list_work_orders(statuses=("pending",),
                                                include_hidden=True):
            meta = db.from_json(candidate.get("metadata"), {}) or {}
            # A hold already in force is the throttle: no `gh` call until it lapses
            if (isinstance(meta.get(self.RELEASE_BATCH_KEY), list)
                    and float(candidate.get("retry_after") or 0) <= db.now()):
                due.append(candidate)
        if not due:
            return

        base = evidence.base_ref(project.path) or "main"
        # `base_runs` asks `gh run list --branch`, which wants the BRANCH
        branch = base.split("/", 1)[1] if base.startswith("origin/") else base
        try:
            # One call for the whole step, `_heal_inherited_red`'s caching by base ref
            runs = ci.base_runs(branch, cwd=project.path)
        except github.GitHubError as e:
            log.debug("[%s] could not read %s's CI, so no release is held: %s",
                      project.name, branch, e)
            return
        red = self._red_base_run(runs)
        for wo in due:
            wo_id = str(wo["id"])
            if red is None:
                # Green with no hold in force writes nothing
                if wo.get("retry_after") is not None and not int(
                        wo.get("dispatch_attempts") or 0):
                    store.hold_dispatch(wo_id, until=None)
                continue
            # §3's threshold, HERE: this tick is the only step that visits a release already held in `pending`
            first = ops.first_red_hold(store, wo_id)
            if first is not None and db.now() - first >= self.RED_PARK_AFTER_SECONDS:
                ops.park_red_release(store, wo_id, {
                    "base": base, "head_sha": red.head_sha, "workflow": red.workflow,
                    "run_id": red.run_id, "run_url": getattr(red, "run_url", "") or ""})
                continue
            until = max(float(wo.get("retry_after") or 0),
                        db.now() + self.RED_HOLD_SECONDS)
            store.hold_dispatch(wo_id, until=until)
            self._say_base_is_red(store, wo_id, base, red, until)

    def _red_base_run(self, runs: tuple[Any, ...]) -> Any | None:
        """The run that says the base is broken right now, or None"""
        from . import ci

        head = runs[0].head_sha if runs else ""
        workflows = tuple(dict.fromkeys(
            r.workflow for r in runs if r.head_sha == head and r.workflow))
        for workflow in workflows:
            latest = ci.latest(runs, workflow)
            if latest is not None and latest.red:
                return latest
        return None

    def _say_base_is_red(self, store: ProjectStore, wo_id: str, base: str,
                         red: Any, until: float) -> None:
        """One line per BROKEN COMMIT on the release order's timeline"""
        head = red.head_sha
        if any(db.from_json(e["payload"], {}).get("head_sha") == head
               for e in store.events_of_kind(wo_id, self.RED_HOLD_EVENT)):
            return
        store.add_event(wo_id, self.RED_HOLD_EVENT, {
            "base": base, "head_sha": head, "run_id": red.run_id,
            "workflow": red.workflow, "until": until,
            "detail": (f"{base} is red at {head[:10]} ({red.workflow}) — holding the "
                       f"release until it is green")})
        log.info("%s is red at %s — holding the release %s", base, head[:10], wo_id)

    #: Said once per TAG on a release order another release overtook (spec §6)
    OVERTAKEN_EVENT = "release_overtaken"

    #: The back-fill of spec §2, on the FIXING order: the commit a merge put on `main`
    MERGE_COMMIT_EVENT = "pr_merge_commit_recorded"

    #: On the DROPPER: one url of its batch that no `jarvis-*` tag carried (#945)
    RELEASE_REFILED_EVENT = "release_refiled"

    #: The other end, on the TARGET: where this entry of its batch came from
    RELEASE_REFILED_FROM_EVENT = "release_refiled_from"

    #: Every url of a settled order's batch is accounted for
    RELEASE_BATCH_CLEARED_EVENT = "release_batch_cleared"

    def settle_shipped_releases(self, project: ProjectSpec,
                                store: ProjectStore) -> None:
        """End a release order whose fixes another release already shipped — issue #784"""
        from . import release
        from .project_store import OPEN_STATUSES

        # §8: the test reads `paths.production_code_dir()` and globs `jarvis-*`
        if project.name != self._os_project():
            return
        orders: list[tuple[dict[str, Any], list[str]]] = []
        for candidate in store.list_work_orders(statuses=OPEN_STATUSES,
                                                include_hidden=True):
            meta = db.from_json(candidate.get("metadata"), {}) or {}
            batch = meta.get(self.RELEASE_BATCH_KEY)
            if isinstance(batch, list) and batch:
                orders.append((candidate, [str(u) for u in batch]))
        if not orders:
            return
        marker = release.read_marker() or {}
        for wo, batch in orders:
            try:
                self._settle_shipped_release(project, store, wo, batch, marker)
            except Exception:  # noqa: BLE001 — one release order must not stall the tick
                log.exception("[%s] settling the release %s failed", project.name,
                              wo["id"])
                continue

    def _settle_shipped_release(self, project: ProjectSpec, store: ProjectStore,
                                wo: dict[str, Any], batch: list[str],
                                marker: dict[str, Any]) -> None:
        """One release order"""
        from . import release

        wo_id = str(wo["id"])
        # §7.5: a marker naming this order, in ANY state including failed_verification
        if str(marker.get("wo_id") or "") == wo_id:
            return
        shas = self._release_payload(project, store, wo_id, batch)
        if shas is None:
            return
        found = release.overtaken_by(project.path, shas)
        if found.error:
            log.debug("[%s] %s: cannot tell whether its batch shipped: %s",
                      project.name, wo_id, found.error)
            return
        if not found.tag:
            return  # §7.3: the ordinary case, and what the order was filed for
        if not found.live:
            # Deduped on the TAG, not the kind: by kind alone this would go silent if the first tag never deploys...
            if any(db.from_json(e["payload"], {}).get("tag") == found.tag
                   for e in store.events_of_kind(wo_id, self.OVERTAKEN_EVENT)):
                return
            store.add_event(wo_id, self.OVERTAKEN_EVENT, {
                "tag": found.tag, "shas": list(shas), "deployed": found.deployed,
                "detail": (f"{found.tag} already carries these fixes; production is on "
                           f"{found.deployed}")})
            log.info("[%s] %s already carries %s's fixes; production is on %s",
                     project.name, found.tag, wo_id, found.deployed)
            return
        settled = release.settle(
            store, wo_id, found.tag,
            why=(f"{found.tag} already carries this release's fixes and production "
                 f"runs it"))
        log.info("[%s] %s shipped %s's batch and is live — %s", project.name,
                 found.tag, wo_id, settled)

    def _release_payload(self, project: ProjectSpec, store: ProjectStore, wo_id: str,
                         batch: list[str]) -> list[str] | None:
        """Every payload commit of this batch, or None if ONE of them cannot be resolved"""
        fixers = {}
        for event in store.events_of_kind(wo_id, "release_batched"):
            said = db.from_json(event["payload"], {})
            fixers[said.get("issue_url")] = said.get("wo_id")
        shas: list[str] = []
        for url in batch:
            fix_id = fixers.get(url)
            if not fix_id:
                return None
            try:
                fix = store.get_work_order(str(fix_id))
            except KeyError:
                return None
            sha = self._merge_commit_of(project, store, fix)
            if not sha:
                return None
            shas.append(sha)
        return shas

    def _merge_commit_of(self, project: ProjectSpec, store: ProjectStore,
                         fix: dict[str, Any]) -> str:
        """The commit this fix put on `main`, or `""` when it cannot be resolved"""
        from . import github

        for kind in ("pr_merged", self.MERGE_COMMIT_EVENT):
            rows = store.events_of_kind(fix["id"], kind)
            if rows:
                sha = db.from_json(rows[-1]["payload"], {}).get("merge_commit") or ""
                if sha:
                    return str(sha)
        pr_url = fix.get("pr_url") or ""
        if not pr_url:
            return ""
        try:
            pr = github.pr_view(pr_url, cwd=project.path)
        except github.GitHubError as e:
            log.debug("[%s] %s: could not read %s for its merge commit: %s",
                      project.name, fix["id"], pr_url, e)
            return ""
        if not (pr.merged and pr.merge_commit_oid):
            return ""
        store.add_event(fix["id"], self.MERGE_COMMIT_EVENT,
                        {"pr_url": pr_url, "merge_commit": pr.merge_commit_oid})
        return pr.merge_commit_oid

    def refile_dropped_fixes(self, project: ProjectSpec, store: ProjectStore) -> None:
        """Re-file a fix a SETTLED release order dropped — issue #945 §2"""
        # §2.1: it reads `jarvis-*` tags and `paths.production_code_dir()`
        if project.name != self._os_owner() or not self.self_dispatch:
            return
        # §2.2: the window is this project's claim about its release cadence
        since = db.now() - int(project.release.refile_window_days) * 86400
        for wo in store.settled_release_orders(since):
            try:
                self._refile_dropped_batch(project, store, wo)
            except Exception:  # noqa: BLE001 — one order must not stall the tick
                log.exception("[%s] re-filing %s's dropped fixes failed", project.name,
                              wo["id"])
                continue

    def _refile_dropped_batch(self, project: ProjectSpec, store: ProjectStore,
                              wo: dict[str, Any]) -> None:
        """One settled order, one decision per url — §2.3"""
        from . import release

        wo_id = str(wo["id"])
        if store.events_of_kind(wo_id, self.RELEASE_BATCH_CLEARED_EVENT):
            return
        meta = db.from_json(wo.get("metadata"), {}) or {}
        batch = [str(u) for u in (meta.get(self.RELEASE_BATCH_KEY) or [])]
        fixers = {}
        for event in store.events_of_kind(wo_id, "release_batched"):
            said = db.from_json(event["payload"], {})
            fixers[said.get("issue_url")] = said.get("wo_id")
        done = {db.from_json(e["payload"], {}).get("issue_url")
                for e in store.events_of_kind(wo_id, self.RELEASE_REFILED_EVENT)}
        accounted = True
        for url in batch:
            if url in done:
                continue
            fix = None
            fix_id = fixers.get(url)
            if fix_id:
                try:
                    fix = store.get_work_order(str(fix_id))
                except KeyError:
                    fix = None
            sha = self._merge_commit_of(project, store, fix) if fix else ""
            if not fix or not sha:
                accounted = False  # §2.3.4: unresolved blocks the clear, so it is re-read
                continue
            found = release.overtaken_by(project.path, [sha])
            # The TAG first: a tag read fine is not an unreadable tag list, whatever production is running
            if found.tag:
                continue
            if found.error:
                log.debug("[%s] %s: cannot tell whether %s shipped: %s", project.name,
                          wo_id, url, found.error)
                accounted = False
                continue
            target = self.ensure_release(project, store, fix)
            if not target:
                # `ensure_release` filing nothing is not a re-file (§2.3.4)
                accounted = False
                continue
            store.add_event(wo_id, self.RELEASE_REFILED_EVENT, {
                "issue_url": url, "release_wo_id": target, "fix_wo_id": str(fix["id"])})
            store.add_event(target, self.RELEASE_REFILED_FROM_EVENT, {
                "issue_url": url, "dropped_by": wo_id})
            log.info("[%s] %s was dropped by %s and no tag carries it — re-filed as %s",
                     project.name, url, wo_id, target)
        if accounted:
            store.add_event(wo_id, self.RELEASE_BATCH_CLEARED_EVENT,
                            {"batch": batch})

    def _warn_issue_sync_broken(self, project: ProjectSpec, store: ProjectStore,
                                error: Exception) -> None:
        """Tell the user once per daemon run that the tracker is not being kept up"""
        from . import github

        if project.name in self.issue_sync_warned:
            return
        self.issue_sync_warned.add(project.name)
        log.warning("[%s] issue lifecycle unavailable: %s", project.name, error)
        hint = ("" if isinstance(error, github.GhUnavailable) else
                " If this is the daemon, `gh`'s keyring credentials may be out of "
                "reach — set GH_TOKEN in the service environment.")
        store.add_notification(
            title=f"the bug tracker is not being kept up to date for {project.name}",
            body=(f"{error}\n\nIssues Jarvis filed will stay as they are — labelled and "
                  f"open — until you close them yourself.{hint}"),
            level="warning", source="issue-sync",
        )

    def _warn_pr_poll_broken(self, project: ProjectSpec, store: ProjectStore,
                             error: Exception) -> None:
        """Tell the user once per daemon run that this project's PRs are not polled (#90)"""
        from . import github

        if project.name in self.pr_poll_warned:
            return
        self.pr_poll_warned.add(project.name)
        log.warning("[%s] pull-request polling unavailable: %s", project.name, error)
        hint = ("" if isinstance(error, github.GhUnavailable) else
                " If this is the daemon, `gh`'s keyring credentials may be out of "
                "reach — set GH_TOKEN in the service environment.")
        store.add_notification(
            title=f"auto-complete on merge is off for {project.name}",
            body=(f"{error}\n\nWork orders parked behind a pull request will stay on "
                  f"the open list until you close them with `jarvis wo done`.{hint}"),
            level="warning", source="pr-poll",
        )

    def retire_ungoverned(self, store: ProjectStore, wo: dict, why: str) -> None:
        """Close an injected session's record without passing judgement on it"""
        store.set_status(wo["id"], "completed")
        store.clear_attention(wo["id"])
        store.add_event(wo["id"], "session_retired", {"why": why})
        log.info("retired injected session %s (%s)", wo["id"], why)

    def _tracking_injected_sessions(self) -> bool:
        """Does any project have a live session Jarvis was handed?"""
        for project in self.catalog.projects:
            if not project.path.is_dir():
                continue
            try:
                store = self.store_for(project)
                if any(wo["origin"] == "injected" for wo in
                       store.list_work_orders(statuses=("running", "waiting_input"),
                                              include_hidden=True)):
                    return True
            except Exception:  # noqa: BLE001 — an unreadable store is the next loop's problem
                log.exception("could not check %s for injected sessions", project.name)
        return False

    def track_injected_sessions(
        self,
        project: ProjectSpec,
        store: ProjectStore,
        sessions_by_cwd: dict[str, list[claude_cli.BgSession]],
    ) -> None:
        """Follow the sessions the user handed to Jarvis, and only those"""
        proot = str(project.path)
        sessions = [
            s for cwd, group in sessions_by_cwd.items()
            if cwd == proot or cwd.startswith(proot + "/")
            for s in group
        ]
        by_session_id = {s.session_id: s for s in sessions if s.session_id}

        for wo in store.list_work_orders(statuses=("running", "waiting_input")):
            if wo["origin"] != "injected":
                continue
            sess = by_session_id.get(wo.get("session_id") or "")
            if sess is None:
                # Gone from the agents view: the user closed it
                if time.time() - wo["updated_at"] > 120:
                    self.retire_ungoverned(store, wo, "session left the agents view")
            elif sess.is_blocked and wo["status"] == "running":
                store.set_status(wo["id"], "waiting_input")
                store.flag_attention(wo["id"],
                                     "session blocked (permission or input needed)")
            elif sess.is_active and wo["status"] != "running":
                store.set_status(wo["id"], "running")
                store.clear_attention(wo["id"])
            elif sess.is_finished:
                self.retire_ungoverned(store, wo, "session went idle")

        # A retired session can start another turn — the user just typed again
        for sess in sessions:
            if not sess.session_id or not sess.is_active:
                continue
            known = store.find_by_session(sess.session_id)
            if (known and known["origin"] == "injected"
                    and known["status"] == "completed"):
                store.set_status(known["id"], "running")


def run_daemon(catalog_path: str | Path, poll_interval: float = 5.0,
               log_to_file: bool = True) -> None:
    ensure_home()
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    if log_to_file:
        handlers.append(logging.FileHandler(logs_dir() / "jarvisd.log"))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=handlers,
    )
    # Before anything is spawned: every worker inherits this process's PATH verbatim... (#41)
    added = bugreport.heal_path()
    if added:
        log.warning("PATH did not include %s — appended for this process and every "
                    "worker it spawns; the installed unit is stale, re-run "
                    "scripts/install_prod_service.sh", ", ".join(added))
    catalog = load_catalog(catalog_path)
    Daemon(catalog, poll_interval=poll_interval).run_forever()


def daemon_running() -> int | None:
    """Return the daemon pid if alive, else None (cleaning up stale pidfiles)"""
    pf = daemon_pidfile()
    if not pf.exists():
        return None
    try:
        pid = int(pf.read_text().strip())
        os.kill(pid, 0)
        return pid
    except (ValueError, ProcessLookupError, PermissionError):
        pf.unlink(missing_ok=True)
        return None
