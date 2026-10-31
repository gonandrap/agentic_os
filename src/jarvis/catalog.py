"""Catalog: the JSON file describing the fleet of projects Jarvis manages"""

from __future__ import annotations

import dataclasses
import json
import math
import zoneinfo
from dataclasses import dataclass, field
from pathlib import Path, PurePosixPath
from typing import Any, Collection

from . import claude_cli
from . import concision
from . import health as health_mod
from . import probes as probes_mod
# The ceiling's constants live in `claude_cli` and are imported HERE (docs/superpowers/specs/2026-09-26-bounded-model-inputs.md)
from .claude_cli import DEFAULT_MAX_OS_PROMPT_CHARS, MAX_OS_PROMPT_CHARS_MIN
from . import schedule as schedule_mod
from .gates import GateConfig
from .session_dispatch import Launcher, parse_launcher
from .navigation import (DOC_DIRS, DOC_DUMP_COMMANDS, DOC_SUFFIXES, NAV_COMMANDS,
                         SOURCE_SUFFIXES, SYMBOL_TOOLS, TEXT_SEARCH_TOOLS)
from .neo_store import Q_KINDS, SEATS
from .project_store import VALIDATOR_SEATS

# Dotted-path globs (fnmatch) over a resolved config map naming the settings that are SAFETY rather... (docs/superpowers/specs/2026-08-27-the-config-console.md §7)
SAFETY_KEYS = (
    "*.permission_mode",
    "*.gates.*",
    # `*.` rather than `os.`: a project's own validation block is the same switch with a smaller blast...
    "*.validation.*",
    "os.neo.enabled",
    # Same class of act as turning Neo off: it removes a reviewer
    "os.supervisor.enabled",
    # And the same again for the health sweep, which is a separate watcher with a separate switch (docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md §2.)
    "os.supervisor.health_enabled",
    # What the OS is permitted to DO, not merely to watch (§5)
    "*.supervisor.remedies.*",
    # The whole scheduler block, not just its switch (docs/superpowers/specs/2026-09-14-the-scheduler.md §2.)
    "*.schedule.*",
    # Files work nobody typed: same class of act as the scheduler
    "os.autonomy.*",
    # How much of that the steward may spend, and how deep it may go (docs/superpowers/specs/2026-10-21-a-steward-decides-what-the-os-may-spend-on-itself.md)
    "os.evolution.weekly_usd", "os.evolution.critical_weekly_usd",
    "os.evolution.yield_above_usd", "os.evolution.order_budget_usd",
    "os.evolution.max_depth", "os.evolution.*_min_refs", "os.evolution.min_refs_by_kind",
    "os.evolution.min_precision",
    "os.evolution.mode",
    # What the OS is permitted to WATCH (docs/superpowers/specs/2026-09-27-self-evolution.md)
    "*.rules.*",
)

# Mirrors `claude --permission-mode` choices exactly (CLI rejects anything else)
VALID_PERMISSION_MODES = {
    "acceptEdits",
    "auto",
    "bypassPermissions",
    "manual",
    "dontAsk",
    "plan",
}

# Default worker mode
DEFAULT_PERMISSION_MODE = "auto"

# Whether `auto` mode's bash-first steer reaches a worker (docs/superpowers/specs/2026-10-01-the-steer-that-beat-the-brief.md)
VALID_BASH_FIRST = ("off", "relaxed", "strict", "cli")
DEFAULT_WORKER_BASH_FIRST = "off"

# Whether a worker's MCP tools are DEFERRED behind `ToolSearch` or listed with full schemas. §4 of... (docs/superpowers/specs/2026-10-02-serena-the-cheap-path.md)
VALID_TOOL_SEARCH = ("off", "on", "cli")
DEFAULT_WORKER_TOOL_SEARCH = "on"

# WHICH PROMPT-CACHE TTL a worker TURN's writes are billed at. §1 of... (docs/superpowers/specs/2026-10-15-the-hour-on-worker-turns-only.md)
VALID_WORKER_CACHE_TTL = ("5m", "1h", "cli")
DEFAULT_WORKER_CACHE_TTL = "5m"
#: What a worker turn runs on: one `claude --bg` session for the conversation (docs/superpowers/specs/2026-10-22-workers-on-background-sessions.md)
VALID_WORKER_TRANSPORT = ("background", "headless")
DEFAULT_WORKER_TRANSPORT = "background"

# Whether `hooks.py_nav_decision` refuses a worker's source-navigating Bash call at a `.py` path. §6... (docs/superpowers/specs/2026-10-02-serena-the-cheap-path.md)
VALID_PY_NAV_HOOK = ("off", "on")
DEFAULT_WORKER_PY_NAV_HOOK = "off"

# Whether `hooks.doc_nav_decision` refuses a whole-file or oversized read of a SPEC (docs/superpowers/specs/2026-10-06-navigate-specs-like-code.md)
VALID_DOC_NAV_HOOK = ("off", "on")
DEFAULT_WORKER_DOC_NAV_HOOK = "off"

# THE BAR BOTH DOC ARMS SHARE, in lines: a `Read` with a larger `limit` (or none at all) and a...
DEFAULT_WORKER_DOC_READ_LIMIT_LINES = 200
# Zero or a negative would make the bar a blanket refusal of every targeted read
WORKER_DOC_READ_LIMIT_LINES_MIN = 1

# Model every worker runs on unless the catalog overrides it (os.defaults.model
DEFAULT_MODEL = "claude-opus-5"

# Modes in which a `--bg` worker never stalls waiting for a human: `auto` (classifier vets each...
AUTONOMOUS_PERMISSION_MODES = {"auto", "bypassPermissions", "dontAsk"}


def worker_stalls_on_prompts(mode: str) -> bool:
    """True when a background worker in this permission mode will block on a prompt"""
    return mode not in AUTONOMOUS_PERMISSION_MODES


# Default simultaneous work orders per project
DEFAULT_MAX_CONCURRENT = 5


# -- the account, which is not a project
DEFAULT_MAX_IN_FLIGHT = 3


# How large a worker's conversation is allowed to grow before Claude Code compacts it (`claude... (docs/superpowers/specs/2026-08-10-resume-cost-and-the-cache.md)
DEFAULT_AUTOCOMPACT_WINDOW = 400_000
# A STANDING DOLLAR BUDGET for every new order in a project, in US dollars, or None for no ceiling
DEFAULT_BUDGET_USD: float | None = None
DEFAULT_FEATURE_BUDGET_USD: float | None = None

# THE ONE NON-None SHIPPED BUDGET (docs/superpowers/specs/2026-09-27-investigation-orders.md)
DEFAULT_INVESTIGATION_BUDGET_USD: float | None = 4.00
# Where 4.00 came from — a string, not a comment, so it is read and tested and never trimmed away
DEFAULT_INVESTIGATION_BUDGET_MEASUREMENT = (
    "Measured 2026-10-21 over the 86 investigator work orders in jarvis_os's project db "
    "(2026-10-01..2026-10-20): first pass (wo_turns.seq=1) min $0.55, median $1.08, "
    "p90 $1.73, max $2.28, 4 truncated at the old $2.00 cap. The old $2.00 was one pass, "
    "so 3 of 3 investigations in io-b521d17b stopped budget_exhausted. Sized at two p90 "
    "passes ($3.46), rounded up. Re-run: per work_orders.kind='investigator', the "
    "wo_turns.cost_usd of seq=1")

# Cheap by default for the same reason: a diagnosis is reading and quoting, not design
DEFAULT_INVESTIGATION_MODEL = "sonnet"
DEFAULT_INVESTIGATION_EFFORT = "low"

# A CEILING ON ONE MCP TOOL CALL (#845)
DEFAULT_WORKER_MCP_TOOL_TIMEOUT_MS = 300_000
# Under a second is a typo — it would make every MCP call in the project fail
WORKER_MCP_TOOL_TIMEOUT_MS_MIN = 1000

#: Built-in tools a one-shot headless turn cannot use (docs/os/architecture/worker-context.md)
DEFAULT_WORKER_DENIED_TOOLS = ("Workflow", "ScheduleWakeup", "ReportFindings",
                               "ListAgents", "Agent(statusline-setup)")


def _parse_budget(raw: dict[str, Any], key: str, where: str,
                  default: float | None) -> float | None:
    """Validate a dollar budget"""
    if key not in raw:
        return default
    value = raw[key]
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise CatalogError(f"{where} must be a dollar amount or null, got {value!r}")
    if not math.isfinite(value):
        raise CatalogError(f"{where} must be a finite dollar amount, got {value!r}")
    if value <= 0:
        raise CatalogError(
            f"{where} must be greater than zero (use null for no ceiling), got {value}")
    return float(value)


AUTOCOMPACT_MIN = 100_000    # `claude --autocompact` rejects anything under this
AUTOCOMPACT_MAX = 1_000_000  # ... or over this

# A cold cache boundary that kept NOTHING was the entry expiring (docs/superpowers/findings/2026-08-30-where-the-800-dollars-went.md)
DEFAULT_COLD_PREFIX_FLOOR = 5_000
#: Guard rail on the above, and configurable for the same reason it is: a fleet whose static heads are...
DEFAULT_COLD_PREFIX_FLOOR_MAX = 100_000

# -- CACHE HEALTH: the window and the floors `invariants.check_cache_ttl_trigger` and

#: The cohort window both checks measure over
DEFAULT_CACHE_HEALTH_WINDOW_DAYS = 30

#: Below this many MEASURED orders in the window, both checks report nothing at all
DEFAULT_CACHE_HEALTH_MIN_ORDERS = 20

#: …and the floor that actually bounds the noise
DEFAULT_CACHE_HEALTH_MIN_BOUNDARIES = 50

#: Prefix-invalidation writes as a share of ALL cache writes
DEFAULT_CACHE_HEALTH_PREFIX_SHARE = 0.45


# -- COMPACTION AT AN EXPIRED BOUNDARY

#: Output tokens one compaction produces
COMPACT_SUMMARY_OUTPUT = 8_052

#: What a session's context weighs on the call AFTER a compaction, in tokens
COMPACT_CONTEXT = 28_156

#: …of which this much is a cache WRITE on that call
COMPACT_FIRST_WRITE = 15_380

#: THE FLOOR: the smallest context worth compacting, in tokens
DEFAULT_COMPACT_MIN_CONTEXT: int | None = 100_000

#: Guard rail on the above, for the reason `DEFAULT_COLD_PREFIX_FLOOR_MAX` is one: a fleet may move...
COMPACT_MIN_CONTEXT_MIN = 10_000

#: How much of a Neo question the DIGEST model is shown (`os.neo.digest_max_question_chars`) (docs/superpowers/specs/2026-09-26-bounded-model-inputs.md)
DEFAULT_DIGEST_MAX_QUESTION_CHARS = 20_000


_MISSING = object()


def _parse_autocompact(raw: dict[str, Any], key: str, where: str,
                       default: int | None) -> int | None:
    """Validate an autocompact window"""
    value = raw.get(key, _MISSING)
    if value is _MISSING:
        return default
    if value is None or value is False:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{where} must be a whole number of tokens or null, "
                         f"got {value!r}")
    if not AUTOCOMPACT_MIN <= value <= AUTOCOMPACT_MAX:
        raise ValueError(
            f"{where} must be between {AUTOCOMPACT_MIN} and {AUTOCOMPACT_MAX} tokens "
            f"(the range `claude --autocompact` accepts), got {value}")
    return value


class CatalogError(ValueError):
    """Raised when the catalog file is invalid"""


@dataclass
class WorkerDefaults:
    model: str | None = None
    effort: str | None = None
    permission_mode: str = DEFAULT_PERMISSION_MODE
    # Spec 2026-10-01-the-steer-that-beat-the-brief.md §1 — see VALID_BASH_FIRST
    bash_first: str = DEFAULT_WORKER_BASH_FIRST
    # Spec 2026-10-02-serena-the-cheap-path.md §4 — see VALID_TOOL_SEARCH
    tool_search: str = DEFAULT_WORKER_TOOL_SEARCH
    # Spec 2026-10-15-the-hour-on-worker-turns-only.md §1 — see VALID_WORKER_CACHE_TTL
    cache_ttl: str = DEFAULT_WORKER_CACHE_TTL
    # Spec 2026-10-22-workers-on-background-sessions.md — see VALID_WORKER_TRANSPORT
    transport: str = DEFAULT_WORKER_TRANSPORT
    # Spec 2026-10-02-serena-the-cheap-path.md §6 — see VALID_PY_NAV_HOOK
    py_nav_hook: str = DEFAULT_WORKER_PY_NAV_HOOK
    # Spec 2026-10-06-navigate-specs-like-code.md §5 — see VALID_DOC_NAV_HOOK
    doc_nav_hook: str = DEFAULT_WORKER_DOC_NAV_HOOK
    # The bar both doc arms share — see DEFAULT_WORKER_DOC_READ_LIMIT_LINES
    doc_read_limit_lines: int = DEFAULT_WORKER_DOC_READ_LIMIT_LINES
    append_system_prompt: str | None = None
    # None = no bound (the model's own window stands)
    autocompact_window: int | None = DEFAULT_AUTOCOMPACT_WINDOW
    # None = no ceiling, which is the default everywhere
    budget_usd: float | None = DEFAULT_BUDGET_USD
    feature_budget_usd: float | None = DEFAULT_FEATURE_BUDGET_USD
    # An INVESTIGATION order's family (the order plus its one investigator) (docs/superpowers/specs/2026-09-27-investigation-orders.md)
    investigation_budget_usd: float | None = DEFAULT_INVESTIGATION_BUDGET_USD
    investigation_model: str | None = DEFAULT_INVESTIGATION_MODEL
    investigation_effort: str | None = DEFAULT_INVESTIGATION_EFFORT
    # Whether the lead must delegate file edits to its crew (docs/superpowers/specs/2026-10-22-workers-on-background-sessions.md)
    require_crew: bool = False
    # The local command that launches this project's sessions (docs/superpowers/specs/2026-10-22-session-dispatch.md)
    launcher: Launcher | None = None
    # What one MCP tool call may take, in milliseconds (#845)
    mcp_tool_timeout_ms: int = DEFAULT_WORKER_MCP_TOOL_TIMEOUT_MS
    denied_tools: tuple[str, ...] = DEFAULT_WORKER_DENIED_TOOLS


# The validation panel's default roster: every seat in the vocabulary
DEFAULT_VALIDATION_ROSTER = VALIDATOR_SEATS

# Per-seat call timeout, seconds
DEFAULT_VALIDATION_TIMEOUT = 300

# How many times a unit may be sent back before the loop gives up and asks a human
DEFAULT_VALIDATION_MAX_ROUNDS = 3

# How many times a work order whose CI has finished RED is sent back WITHOUT seating the panel and... (docs/superpowers/specs/2026-10-13-a-red-head-is-sent-back-before-the-panel.md)
DEFAULT_VALIDATION_RED_CI_RETURNS = 3

# How many times a round whose seats were KILLED BY A SIGNAL is re-judged before the OS gives up on... (docs/superpowers/specs/2026-10-14-a-killed-validation-panel-opens-an-investigation.md)
DEFAULT_VALIDATION_KILLED_RETRIES = 3

# Truncation limit for the diff a seat is shown (#206)
DEFAULT_VALIDATION_DIFF_CHARS = 150000

# Characters of DECISION RECORD one assumption-review packet may carry (docs/superpowers/specs/2026-09-28-an-assumption-review-reads-the-orders-own-rulings.md)
DEFAULT_VALIDATION_DECISION_RECORD_CHARS = 6000

# Characters of SPEC SECTION the same packet may carry (#1035)
DEFAULT_VALIDATION_ASSUMPTION_SPEC_CHARS = 6000

# How many follow-up findings ONE ORDER may file, across every round it is judged in (docs/superpowers/specs/2026-09-15-the-panel-blocks-on-blockers.md)
DEFAULT_VALIDATION_FOLLOW_UP_CAP = 5

# How long the reconciler may wait for a merged child's commit to appear on the default branch before... (docs/superpowers/specs/2026-10-07-a-feature-round-must-judge-a-head-that-contains-its-children.md)
DEFAULT_VALIDATION_FEATURE_MERGE_WAIT_MINUTES = 15

# Withdrawn share of early objections that reaches the inbox, 1 = off (io-ce566253)
DEFAULT_VALIDATION_OBJECTION_WITHDRAWAL_ALARM_RATE = 0.5

#: Which net answers "is this assumption high-stakes?" before any model call... (docs/superpowers/specs/2026-09-25-a-model-decides-what-is-high-stakes.md)
STAKES_CLASSIFIER_MODES = ("regex", "shadow", "classifier", "regex-tightened")
# The measured winner (spec 2026-09-25 §7: precision 0.486 / recall 0.944 vs regex 0.140 / 0.667)
DEFAULT_STAKES_CLASSIFIER = "regex-tightened"


@dataclass
class ValidationConfig:
    """An independent panel judging a work order's or feature order's claim of done (docs/superpowers/specs/2026-09-15-the-panel-blocks-on-blockers.md §5.4)"""

    enabled: bool = False
    roster: tuple[str, ...] = DEFAULT_VALIDATION_ROSTER
    seat_models: dict[str, str] = field(default_factory=dict)
    chair_model: str = ""
    timeout: int = DEFAULT_VALIDATION_TIMEOUT
    max_rounds: int = DEFAULT_VALIDATION_MAX_ROUNDS
    # Spec §4: docs/superpowers/specs/2026-10-13-a-red-head-is-sent-back-before-the-panel.md
    red_ci_returns: int = DEFAULT_VALIDATION_RED_CI_RETURNS
    # Spec §4b: docs/superpowers/specs/2026-10-14-a-killed-validation-panel-opens-an-investigation.md
    killed_retries: int = DEFAULT_VALIDATION_KILLED_RETRIES
    diff_chars: int = DEFAULT_VALIDATION_DIFF_CHARS
    decision_record_chars: int = DEFAULT_VALIDATION_DECISION_RECORD_CHARS
    # GitHub issue #1035: the spec section an assumption review reads
    assumption_spec_chars: int = DEFAULT_VALIDATION_ASSUMPTION_SPEC_CHARS
    # Whether a FEATURE order validates as a whole once its children are done
    feature_units: bool = True
    # §3 of... (docs/superpowers/specs/2026-10-07-a-feature-round-must-judge-a-head-that-contains-its-children.md)
    feature_merge_wait_minutes: int = DEFAULT_VALIDATION_FEATURE_MERGE_WAIT_MINUTES
    # WHETHER THE OS MAY MERGE THIS PROJECT'S PULL REQUESTS ITSELF (docs/superpowers/specs/2026-09-14-validated-auto-merge-design.md)
    auto_merge: bool = False
    # WHETHER THE OS MAY DECIDE THIS PROJECT'S PENDING ASSUMPTIONS ITSELF (docs/superpowers/specs/2026-09-15-neo-decides-an-assumption.md)
    auto_review: bool = False
    # WHETHER A NON-BLOCKING FINDING IS KEPT AS A BACKLOG ITEM, or discarded (docs/superpowers/specs/2026-09-15-the-panel-blocks-on-blockers.md)
    follow_ups: bool = True
    # The cap above, per project
    max_follow_ups: int = DEFAULT_VALIDATION_FOLLOW_UP_CAP
    # WHICH NET DECIDES THAT AN ASSUMPTION IS HIGH-STAKES before any model call... (docs/superpowers/specs/2026-09-25-a-model-decides-what-is-high-stakes.md)
    stakes_classifier: str = DEFAULT_STAKES_CLASSIFIER
    # WHEN FAILING EARLY OBJECTIONS REACH THE INBOX: withdrawn over sent (io-ce566253)
    objection_withdrawal_alarm_rate: float = DEFAULT_VALIDATION_OBJECTION_WITHDRAWAL_ALARM_RATE


# -- the house style: how terse the OS holds its workers


@dataclass(frozen=True)
class ConcisionConfig:
    """The one number the concision machinery needs per project (docs/superpowers/specs/2026-09-19-concision-enforced.md)"""

    summary_max_words: int = concision.DEFAULT_SUMMARY_MAX_WORDS
    #: The same bargain one surface over: `message_max_chars` = 0 turns the `jarvis wo send`... (docs/superpowers/specs/2026-09-26-bounded-model-inputs.md)
    message_max_chars: int = concision.DEFAULT_MESSAGE_MAX_CHARS
    #: ONE DECISION per assumption, ONE question per ask: both 0 switch their check off (docs/superpowers/specs/2026-10-21-questions-to-neo-are-atomic.md §1)
    assume_max_chars: int = concision.DEFAULT_ASSUME_MAX_CHARS
    question_max_chars: int = concision.DEFAULT_QUESTION_MAX_CHARS


# -- `jarvis inspect`: what counts as worth reporting, and what as worth interrupting for

#: A cache write below this is the conversation's own growth rather than a re-send of it (docs/findings/anatomy-of-an-expensive-turn.md)
DEFAULT_INSPECT_REPORT_WRITE_FLOOR = 20_000

#: A blocking join shorter than this is not worth a line of its own
DEFAULT_INSPECT_REPORT_JOIN_FLOOR = 30

#: How much of a turn's triggering prompt is quoted
DEFAULT_INSPECT_QUOTE_CHARS = 140

#: A turn still running after this long is burning money now
DEFAULT_INSPECT_ALARM_TURN_MINUTES = 60

#: A turn that has been open this long WITHOUT MAKING A SINGLE API CALL
DEFAULT_INSPECT_ALARM_STALLED_MINUTES = 15

#: A request in flight this long with no completed content block (docs/superpowers/specs/2026-09-27-a-request-in-flight-is-not-a-stalled-turn.md §2.)
DEFAULT_INSPECT_ALARM_AWAITING_MINUTES = 60

#: A blocking join still open after this long
DEFAULT_INSPECT_ALARM_JOIN_SECONDS = 300

#: A subagent's last tool call still unfinished after this long (#845)
DEFAULT_INSPECT_ALARM_SUBAGENT_TOOL_MINUTES = 5

#: One call re-sending this much of the conversation. p95 of the largest re-write per work order (the...
DEFAULT_INSPECT_ALARM_WRITE_TOKENS = 300_000

#: How long a work order may sit with its turn ended and nothing in flight before...
DEFAULT_INSPECT_ALARM_PARKED_MINUTES = 60

#: How far back the compaction post-conditions look for a chained or unproven compaction (io-12b7d873)
DEFAULT_INSPECT_ALARM_COMPACTION_WINDOW_DAYS = 7

# -- the AGGREGATE half of the re-write tax

#: The cohort window, in days
DEFAULT_INSPECT_ALARM_REWRITE_WINDOW_DAYS = 7

#: What fraction of the window's bill went on re-sending conversations whose PROMPT PREFIX had moved
DEFAULT_INSPECT_ALARM_REWRITE_PREFIX_SHARE = 0.15

#: The same fraction for the other cause: the cache entry EXPIRING
DEFAULT_INSPECT_ALARM_REWRITE_TTL_SHARE = 0.15

#: A share computed over fewer settled orders than this is one order's shape rather than the project's
DEFAULT_INSPECT_ALARM_REWRITE_MIN_ORDERS = 5

#: …and below this much spend in the window a percentage is arithmetic on noise
DEFAULT_INSPECT_ALARM_REWRITE_MIN_USD = 10.0

# -- who is still buying the ONE-HOUR cache write

#: The window, in days
DEFAULT_INSPECT_ALARM_CACHE_1H_WINDOW_DAYS = 7

#: One-hour tokens written in the window by 5m-armed turns JARVIS ITSELF dispatched
DEFAULT_INSPECT_ALARM_CACHE_1H_DISPATCHED_TOKENS = 50_000

#: Tokens a STAMPED turn paid at the TTL it was NOT armed for (docs/superpowers/specs/2026-10-22-the-arm-that-did-not-apply.md)
DEFAULT_INSPECT_ALARM_CACHE_ARM_MISSED_TOKENS = 50_000

#: `InspectConfig` fields that are a FRACTION, not a count
INSPECT_FRACTION_KEYS = ("alarm_rewrite_prefix_share", "alarm_rewrite_ttl_share")

#: …and the one that is money
INSPECT_MONEY_KEYS = ("alarm_rewrite_min_usd",)


@dataclass
class InspectConfig:
    """What `jarvis inspect` reports, and when the OS raises a turn that is still running"""

    enabled: bool = True
    report_write_floor: int = DEFAULT_INSPECT_REPORT_WRITE_FLOOR
    report_join_floor: int = DEFAULT_INSPECT_REPORT_JOIN_FLOOR
    quote_chars: int = DEFAULT_INSPECT_QUOTE_CHARS
    alarm_turn_minutes: int = DEFAULT_INSPECT_ALARM_TURN_MINUTES
    alarm_stalled_minutes: int = DEFAULT_INSPECT_ALARM_STALLED_MINUTES
    alarm_awaiting_minutes: int = DEFAULT_INSPECT_ALARM_AWAITING_MINUTES
    alarm_join_seconds: int = DEFAULT_INSPECT_ALARM_JOIN_SECONDS
    alarm_subagent_tool_minutes: int = DEFAULT_INSPECT_ALARM_SUBAGENT_TOOL_MINUTES
    alarm_write_tokens: int = DEFAULT_INSPECT_ALARM_WRITE_TOKENS
    alarm_parked_minutes: int = DEFAULT_INSPECT_ALARM_PARKED_MINUTES
    alarm_compaction_window_days: int = DEFAULT_INSPECT_ALARM_COMPACTION_WINDOW_DAYS
    alarm_rewrite_window_days: int = DEFAULT_INSPECT_ALARM_REWRITE_WINDOW_DAYS
    alarm_rewrite_prefix_share: float = DEFAULT_INSPECT_ALARM_REWRITE_PREFIX_SHARE
    alarm_rewrite_ttl_share: float = DEFAULT_INSPECT_ALARM_REWRITE_TTL_SHARE
    alarm_rewrite_min_orders: int = DEFAULT_INSPECT_ALARM_REWRITE_MIN_ORDERS
    alarm_rewrite_min_usd: float = DEFAULT_INSPECT_ALARM_REWRITE_MIN_USD
    alarm_cache_1h_window_days: int = DEFAULT_INSPECT_ALARM_CACHE_1H_WINDOW_DAYS
    alarm_cache_1h_dispatched_tokens: int = \
        DEFAULT_INSPECT_ALARM_CACHE_1H_DISPATCHED_TOKENS
    alarm_cache_arm_missed_tokens: int = \
        DEFAULT_INSPECT_ALARM_CACHE_ARM_MISSED_TOKENS


# -- `ui_health`: the dashboard's own liveness, and the self-heal that answers it

#: Seconds between liveness probes
DEFAULT_UI_HEALTH_PROBE_INTERVAL_SECONDS = 15

#: How long one probe may wait for the threadpool
DEFAULT_UI_HEALTH_PROBE_TIMEOUT_SECONDS = 20

#: Consecutive failed probes before the process dumps its own stacks
DEFAULT_UI_HEALTH_TRIP_THRESHOLD = 3

#: The DAEMON's own deadline on `GET /healthz`
DEFAULT_UI_HEALTH_HEALTHZ_TIMEOUT_SECONDS = 5

#: No self-restart within this long of the last one
DEFAULT_UI_HEALTH_RESTART_COOLDOWN_SECONDS = 300

#: Self-restarts allowed in a rolling 24h
DEFAULT_UI_HEALTH_MAX_RESTARTS_PER_DAY = 3

#: An unreachable dashboard this recently (re)started is still booting, not wedged
DEFAULT_UI_HEALTH_BOOT_GRACE_SECONDS = 60

#: Heartbeat staleness at which the OS-thread watchdog dumps stacks itself (issue #1221)
DEFAULT_UI_HEALTH_LOOP_STALL_SECONDS = 90


@dataclass
class UiHealthConfig:
    """How the dashboard reports its own wedge, and when the OS restarts it"""

    enabled: bool = True
    probe_interval_seconds: int = DEFAULT_UI_HEALTH_PROBE_INTERVAL_SECONDS
    probe_timeout_seconds: int = DEFAULT_UI_HEALTH_PROBE_TIMEOUT_SECONDS
    trip_threshold: int = DEFAULT_UI_HEALTH_TRIP_THRESHOLD
    healthz_timeout_seconds: int = DEFAULT_UI_HEALTH_HEALTHZ_TIMEOUT_SECONDS
    restart_cooldown_seconds: int = DEFAULT_UI_HEALTH_RESTART_COOLDOWN_SECONDS
    max_restarts_per_day: int = DEFAULT_UI_HEALTH_MAX_RESTARTS_PER_DAY
    boot_grace_seconds: int = DEFAULT_UI_HEALTH_BOOT_GRACE_SECONDS
    loop_stall_seconds: int = DEFAULT_UI_HEALTH_LOOP_STALL_SECONDS


# -- `fleet_health`: when an order that has stopped moving gets investigated. §4 of

#: Ships OFF: every investigation it opens is spend nobody typed (os.autonomy)
DEFAULT_FLEET_HEALTH_ENABLED = False

#: Minutes in status before one open status is over threshold
DEFAULT_FLEET_HEALTH_THRESHOLDS: dict[str, int] = {
    "pending": 30,
    "dispatching": 5,
    "running": 5,
    "validating": 15,
    "needs_review": 60,
    "waiting_pr_merge": 60,
    "waiting_input": 60,
}

#: The answer for an open status the mapping does not name
DEFAULT_FLEET_HEALTH_FALLBACK_MINUTES = 30

#: How long one subject waits before a second investigation, Neo 1073
DEFAULT_FLEET_HEALTH_COOLDOWN_MINUTES = 720

#: Investigations the sweep may open in a day, FLEET-WIDE and counted from the records themselves (§6c)
DEFAULT_FLEET_HEALTH_MAX_PER_DAY = 48

#: Ticks between sweeps of one project — 5 minutes at the default 5s `poll_interval`
DEFAULT_FLEET_HEALTH_SWEEP_EVERY_TICKS = 60

#: How long the stuck sweep itself may be silent before the user is told...
DEFAULT_FLEET_HEALTH_SWEEP_DARK_MINUTES = 180

#: How long one project's red-base episodes are REMEMBERED (docs/superpowers/specs/2026-10-15-a-merge-held-by-a-red-base-is-not-stuck.md)
DEFAULT_FLEET_HEALTH_RED_EPISODE_RETENTION_HOURS = 168

#: How long a red default branch may heal itself before the OS pays for a fix (wo-6bc0cd90, Neo 2227)
DEFAULT_FLEET_HEALTH_BASE_FIX_GRACE_MINUTES = 30

#: `FleetHealthConfig` fields that are a MAPPING, not a count — `INSPECT_FRACTION_KEYS`' arrangement
FLEET_HEALTH_MAP_KEYS = ("thresholds",)

#: Work-order kinds the sweep never judges
DEFAULT_FLEET_HEALTH_EXEMPT_KINDS: tuple[str, ...] = ("investigator",)

#: `FleetHealthConfig` fields that are a LIST, not a count
FLEET_HEALTH_LIST_KEYS = ("exempt_kinds",)

#: `FleetHealthConfig` fields that are a FLEET number and are REFUSED on a project
FLEET_HEALTH_FLEET_ONLY_KEYS: dict[str, str] = {
    "max_per_day": "No arrangement of per-project numbers can ration a fleet-wide "
                   "daily cap.",
    "sweep_dark_minutes": "The stuck sweep writes ONE fleet-wide run record, so no "
                          "arrangement of per-project numbers can say how long that one "
                          "record may be silent.",
}


@dataclass
class FleetHealthConfig:
    """When the OS decides one order has stopped moving, and how often it may say so"""

    enabled: bool = DEFAULT_FLEET_HEALTH_ENABLED
    thresholds: dict[str, int] = field(
        default_factory=lambda: dict(DEFAULT_FLEET_HEALTH_THRESHOLDS))
    fallback_minutes: int = DEFAULT_FLEET_HEALTH_FALLBACK_MINUTES
    cooldown_minutes: int = DEFAULT_FLEET_HEALTH_COOLDOWN_MINUTES
    max_per_day: int = DEFAULT_FLEET_HEALTH_MAX_PER_DAY
    #: Neo 1086: how often this project is swept, per project rather than a constant
    sweep_every_ticks: int = DEFAULT_FLEET_HEALTH_SWEEP_EVERY_TICKS
    #: Fleet-only: how long the sweep's own run record may be silent (§8)
    sweep_dark_minutes: int = DEFAULT_FLEET_HEALTH_SWEEP_DARK_MINUTES
    #: Kinds this project's sweep never judges
    exempt_kinds: tuple[str, ...] = DEFAULT_FLEET_HEALTH_EXEMPT_KINDS
    #: §2 of docs/superpowers/specs/2026-10-15-a-merge-held-by-a-red-base-is-not-stuck.md
    red_episode_retention_hours: int = DEFAULT_FLEET_HEALTH_RED_EPISODE_RETENTION_HOURS
    #: Minutes `main` stays red before `dispatch_base_fix` asks the steward
    base_fix_grace_minutes: int = DEFAULT_FLEET_HEALTH_BASE_FIX_GRACE_MINUTES

    def threshold_seconds(self, status: str) -> float:
        """This status's threshold, in the seconds `stuck.assess` compares"""
        return float(self.thresholds.get(status, self.fallback_minutes)) * 60.0


# -- `jarvis navigation`: what counts as NAVIGATION, as data rather than code. q1216

# §3 of the 2026-10-02 navigation split: ONE definition, in the stdlib-only leaf

#: Bash commands that count as reading or searching code
DEFAULT_NAVIGATION_BASH_COMMANDS = NAV_COMMANDS

#: Symbol tools, BARE — `navigation.is_symbol_call` strips the `mcp__<server>__` prefix
DEFAULT_NAVIGATION_SYMBOL_TOOLS = SYMBOL_TOOLS

#: The TOOLS that are text search
DEFAULT_NAVIGATION_TEXT_SEARCH_TOOLS = TEXT_SEARCH_TOOLS

#: Which files make a read a CODE read
DEFAULT_NAVIGATION_CODE_SUFFIXES = SOURCE_SUFFIXES

#: Which files make a read a DOC read (docs/superpowers/specs/2026-10-06-navigate-specs-like-code.md §3.2.)
DEFAULT_NAVIGATION_DOC_SUFFIXES = DOC_SUFFIXES

#: Which path components make a file a DOC by location
DEFAULT_NAVIGATION_DOC_DIRS = DOC_DIRS

#: The Bash commands that DUMP a doc (docs/superpowers/specs/2026-10-06-navigate-specs-like-code.md §3.2.)
DEFAULT_NAVIGATION_DOC_DUMP_COMMANDS = DOC_DUMP_COMMANDS

#: The default window for a wide scope, in days
DEFAULT_NAVIGATION_WINDOW_DAYS = 7

#: The `NavigationConfig` fields that are a PATTERN LIST
NAVIGATION_PATTERN_KEYS = ("bash_commands", "symbol_tools", "text_search_tools",
                           "code_suffixes", "doc_suffixes", "doc_dirs",
                           "doc_dump_commands")


@dataclass
class NavigationConfig:
    """What `jarvis navigation` calls navigation, and over how long"""

    enabled: bool = True
    bash_commands: tuple[str, ...] = DEFAULT_NAVIGATION_BASH_COMMANDS
    symbol_tools: tuple[str, ...] = DEFAULT_NAVIGATION_SYMBOL_TOOLS
    text_search_tools: tuple[str, ...] = DEFAULT_NAVIGATION_TEXT_SEARCH_TOOLS
    code_suffixes: tuple[str, ...] = DEFAULT_NAVIGATION_CODE_SUFFIXES
    doc_suffixes: tuple[str, ...] = DEFAULT_NAVIGATION_DOC_SUFFIXES
    doc_dirs: frozenset[str] = DEFAULT_NAVIGATION_DOC_DIRS
    doc_dump_commands: tuple[str, ...] = DEFAULT_NAVIGATION_DOC_DUMP_COMMANDS
    window_days: int = DEFAULT_NAVIGATION_WINDOW_DAYS


#: Shipped defaults of `CostConfig`
DEFAULT_COST_WEEK_RESET_WEEKDAY = 0        # Monday, `datetime.weekday()` numbering
DEFAULT_COST_WEEK_RESET_HOUR = 21
DEFAULT_COST_WEEK_RESET_ZONE = "America/Los_Angeles"
DEFAULT_COST_PERCENTILE = 0.9
DEFAULT_COST_MAX_ORDERS = 500
#: The `chars` fallback's divisor, where the exact `context-delta` basis does not apply
DEFAULT_COST_CHARS_PER_TOKEN = 4.0
DEFAULT_COST_TOOL_ROWS = 20
#: The 5h grid's length, a SLICE OF THE WEEK and not Anthropic's session accounting (docs/superpowers/specs/2026-10-07-cost-window-selector.md)
DEFAULT_COST_SESSION_WINDOW_HOURS = 5.0
#: The usage meter — §§4, 5 and 8 of... (docs/superpowers/specs/2026-10-08-usage-meter-samples-and-outside-spend.md)
DEFAULT_COST_METER_NEAREST_SECONDS = 300
DEFAULT_COST_METER_CALIBRATION_MIN_MINUTES = 30
DEFAULT_COST_METER_CALIBRATION_MIN_POINTS = 5
DEFAULT_COST_METER_CALIBRATION_DAYS = 7
#: Measured on this machine 2026-10-08: ~$0.82 of list-price spend per 5h point
DEFAULT_COST_METER_DOLLARS_PER_POINT = 0.82
#: Fifteen consecutive failed minutes is a broken token or a broken network, where three is a blip
DEFAULT_COST_METER_STALE_MINUTES = 15
DEFAULT_COST_METER_GAP_NOTICE_SAMPLES = 10
#: Rows of the outside-session table SHOWN
DEFAULT_COST_METER_OUTSIDE_ROWS = 10
#: The two alarm shares and the floor under both (§8)
DEFAULT_COST_METER_OUTSIDE_ALERT_SHARE = 0.25
DEFAULT_COST_METER_RESIDUAL_ALERT_SHARE = 0.25
DEFAULT_COST_METER_ALERT_MIN_USD = 5.0


@dataclass
class CostConfig:
    """What `jarvis cost --fleet` reports over, and where its default window starts (docs/superpowers/specs/2026-10-06-fleet-cost-distribution.md)"""

    week_reset_weekday: int = DEFAULT_COST_WEEK_RESET_WEEKDAY
    week_reset_hour: int = DEFAULT_COST_WEEK_RESET_HOUR
    week_reset_zone: str = DEFAULT_COST_WEEK_RESET_ZONE
    percentile: float = DEFAULT_COST_PERCENTILE
    max_orders: int = DEFAULT_COST_MAX_ORDERS
    chars_per_token: float = DEFAULT_COST_CHARS_PER_TOKEN
    tool_rows: int = DEFAULT_COST_TOOL_ROWS
    session_window_hours: float = DEFAULT_COST_SESSION_WINDOW_HOURS
    meter_nearest_seconds: int = DEFAULT_COST_METER_NEAREST_SECONDS
    meter_calibration_min_minutes: int = DEFAULT_COST_METER_CALIBRATION_MIN_MINUTES
    meter_calibration_min_points: float = DEFAULT_COST_METER_CALIBRATION_MIN_POINTS
    meter_calibration_days: int = DEFAULT_COST_METER_CALIBRATION_DAYS
    meter_dollars_per_point: float = DEFAULT_COST_METER_DOLLARS_PER_POINT
    meter_stale_minutes: int = DEFAULT_COST_METER_STALE_MINUTES
    meter_gap_notice_samples: int = DEFAULT_COST_METER_GAP_NOTICE_SAMPLES
    meter_outside_rows: int = DEFAULT_COST_METER_OUTSIDE_ROWS
    meter_outside_alert_share: float = DEFAULT_COST_METER_OUTSIDE_ALERT_SHARE
    meter_residual_alert_share: float = DEFAULT_COST_METER_RESIDUAL_ALERT_SHARE
    meter_alert_min_usd: float = DEFAULT_COST_METER_ALERT_MIN_USD


#: The legal observability levels and the shipped default, as LITERALS (docs/superpowers/specs/2026-09-24-order-observability.md §10.)
OBSERVABILITY_LEVELS = ("off", "normal", "full")
DEFAULT_OBSERVABILITY_LEVEL = "normal"


@dataclass
class ObservabilityConfig:
    """What debug data Jarvis COLLECTS. §10 of
    docs/superpowers/specs/2026-09-24-order-observability.md.

    Per project as well as fleet-wide, with `_parse_inspect`'s field-level inheritance
    (`_parse_observability`), and a per-order override on `work_orders.observability`
    beats both — precedence resolved in one place, `observability.level_for`.

    `off` GATES TWO WRITES: §5's per-turn ingredient row on `wo_turns.context_json` and
    the sealed autopsy (§5 of docs/superpowers/specs/2026-09-27-order-autopsy-durability.md). So `off`
    stops the autopsy being sealed and does NOT disable `jarvis watch`, `jarvis inspect`,
    `jarvis wo why` or the debug page — those are arithmetic over files that already
    exist, so gating them would remove the view and save nothing. The consequence at
    `off` is that the order has no context ledger and no sealed autopsy, and `jarvis wo
    context` says it was not recorded.

    `full` DIFFERS FROM `normal` BY EXACTLY ONE THING: the tool parameters a `full` seal
    retains (§6 of docs/superpowers/specs/2026-09-27-order-autopsy-durability.md). The autopsy READING
    itself — every turn, its tools, its token classes, its context total, delta, peak and
    composition — is derived at read time from the transcript (§§3, 4, 6, 7) and so is
    shown for every order at every level, `off` included.
    """

    level: str = DEFAULT_OBSERVABILITY_LEVEL


# -- message delivery: how long a queued message may stay undelivered before the OS

#: `Daemon.deliver_messages` sends within one reconcile tick, so this is not a latency bar
DEFAULT_MESSAGING_STUCK_MINUTES = 60


# -- the bug lifecycle: what happens to a GitHub issue `jarvis bug report` files

#: The label that says the OS has picked an issue up (#240)
DEFAULT_BUGS_LABEL = "in progress"


@dataclass
class BugsConfig:
    """How the tracker is labelled while the OS works on a bug it filed"""

    label: str = DEFAULT_BUGS_LABEL


#: How far back `Daemon.refile_dropped_fixes` looks
DEFAULT_RELEASE_REFILE_WINDOW_DAYS = 14


@dataclass
class ReleaseConfig:
    """How long a settled release order's dropped batch stays worth re-filing"""

    refile_window_days: int = DEFAULT_RELEASE_REFILE_WINDOW_DAYS


@dataclass
class AutonomyConfig:
    """Whether the OS may create orders nobody typed (bug pickups, riders, red-main fixes, releases)"""

    self_dispatch: bool = False


def _parse_autonomy(raw: Any) -> AutonomyConfig:
    if not isinstance(raw, dict):
        raise _err('"os.autonomy" must be an object')
    value = raw.get("self_dispatch", False)
    if not isinstance(value, bool):
        raise _err(f"os.autonomy.self_dispatch must be true or false, got {value!r}")
    return AutonomyConfig(self_dispatch=value)


@dataclass
class MessagingConfig:
    """When a message queued for a worker stops being in flight and becomes a defect"""

    stuck_minutes: int = DEFAULT_MESSAGING_STUCK_MINUTES


# -- pull requests: how long GitHub may go without recomputing one before the OS stops

#: Half an hour (#995)
DEFAULT_PULL_REQUESTS_RECOMPUTE_STALL_MINUTES = 30

#: How many times the OS may catch one pull request up with a MOVING base...
DEFAULT_PULL_REQUESTS_CATCH_UP_MAX = 10
#: ...within this window: a RATE, not a lifetime budget (#1215, Neo 2191)
DEFAULT_PULL_REQUESTS_CATCH_UP_WINDOW_MINUTES = 120
#: The wait after the first catch-up of a window, doubled after each one since...
DEFAULT_PULL_REQUESTS_CATCH_UP_BACKOFF_BASE_SECONDS = 60
#: ...and never longer than this between two catch-ups
DEFAULT_PULL_REQUESTS_CATCH_UP_BACKOFF_MAX_SECONDS = 600


@dataclass
class PullRequestsConfig:
    """When GitHub's silence about a pull request stops being a wait and becomes a stall"""

    recompute_stall_minutes: int = DEFAULT_PULL_REQUESTS_RECOMPUTE_STALL_MINUTES
    #: How often the OS catches one branch up with its base, and how far apart (`ops.catch_up_backoff`)
    catch_up_max: int = DEFAULT_PULL_REQUESTS_CATCH_UP_MAX
    catch_up_window_minutes: int = DEFAULT_PULL_REQUESTS_CATCH_UP_WINDOW_MINUTES
    catch_up_backoff_base_seconds: int = DEFAULT_PULL_REQUESTS_CATCH_UP_BACKOFF_BASE_SECONDS
    catch_up_backoff_max_seconds: int = DEFAULT_PULL_REQUESTS_CATCH_UP_BACKOFF_MAX_SECONDS


# -- the scheduler: the OS filing a work order nobody typed

#: Daily, which is the cadence the ask named
DEFAULT_SCHEDULE_INTERVAL_HOURS = 24

#: How many whole intervals a job may sit HELD
DEFAULT_SCHEDULE_HELD_ALARM_INTERVALS = 3


@dataclass
class WiringConfig:
    """Which of the USER'S OWN MCP servers, skills and plugins reach this project's workers (docs/superpowers/specs/2026-09-16-per-project-wiring.md §2)"""

    claude_ai_connectors: bool = True
    # Off by default: CLI-shipped skills (dataviz, loop, …) cost ~1.6K tokens per worker start
    bundled_skills: bool = False
    #: `<plugin>@<marketplace>` ids, as `claude plugin list` spells them
    disabled_plugins: tuple[str, ...] = ()
    #: Skill names — user and project skills only
    disabled_skills: tuple[str, ...] = ()
    # Off adds `--no-chrome`: the user's global config, not settings, turns Claude in Chrome on (issue 1370)
    chrome: bool = True


#: Tracked files an installed TOOL owns and rewrites
DEFAULT_TOOL_MANAGED_PATHS = (".serena/project.yml",)

#: How long a settled order's checkout is kept before the daemon reclaims it; 0 = never (docs/superpowers/specs/2026-10-22-a-settled-checkout-is-reclaimed.md)
DEFAULT_WORKTREE_RECLAIM_AFTER_HOURS = 24

#: Free gigabytes under which the daemon raises ONE attention item; 0 = never (same spec, §2)
DEFAULT_DISK_FREE_WARN_GB = 5.0


@dataclass
class WorktreeConfig:
    """What is true of a WORKER'S worktree specifically, and not of the shared checkout"""

    #: Repo-relative paths
    tool_managed_paths: tuple[str, ...] = DEFAULT_TOOL_MANAGED_PATHS
    reclaim_after_hours: int = DEFAULT_WORKTREE_RECLAIM_AFTER_HOURS


@dataclass
class ScheduleConfig:
    """Recurring work orders: whether, how often, and which"""

    enabled: bool = False
    interval_hours: int = DEFAULT_SCHEDULE_INTERVAL_HOURS
    jobs: tuple[str, ...] = schedule_mod.JOB_IDS
    held_alarm_intervals: int = DEFAULT_SCHEDULE_HELD_ALARM_INTERVALS

    @property
    def interval_seconds(self) -> float:
        return self.interval_hours * schedule_mod.SECONDS_PER_HOUR


# -- the supervisor: it JUDGES a cost alarm, so every number it judges by is a setting

DEFAULT_SUPERVISOR_MODEL = "opus"
DEFAULT_SUPERVISOR_TIMEOUT = 300
DEFAULT_SUPERVISOR_LEARNINGS_LIMIT = 50

#: Past this an alarm is skipped unjudged: spend the user can no longer prevent
DEFAULT_SUPERVISOR_MAX_AGE_HOURS = 24

#: MUST EXCEED `timeout`, or a claim is reclaimed out from under a call that is still running and one...
DEFAULT_SUPERVISOR_STALE_REVIEWING_SECONDS = 900
DEFAULT_SUPERVISOR_MAX_REVIEW_ATTEMPTS = 3

#: The evidence packet's ceiling and the clips that fill it
DEFAULT_SUPERVISOR_EVIDENCE_BUDGET_CHARS = 8000
DEFAULT_SUPERVISOR_CONVERSATION_QUOTE_CHARS = 400
DEFAULT_SUPERVISOR_QUOTED_TURNS = 3
DEFAULT_SUPERVISOR_DESCRIPTION_CHARS = 500

#: What the user is told, and how much of an unusable reply is kept as the reason
DEFAULT_SUPERVISOR_NOTE_CHARS = 200
DEFAULT_SUPERVISOR_REASON_CHARS = 200

# -- the health sweep's numbers, created here so §4 touches no catalog code and the two

#: How often the sweep runs, and the floor between two looks at the SAME unit
DEFAULT_SUPERVISOR_HEALTH_EVERY_TICKS = 20
DEFAULT_SUPERVISOR_HEALTH_MIN_INTERVAL_MINUTES = 30

#: How long a unit's fingerprint may sit still before that stillness is itself the thing worth looking...
DEFAULT_SUPERVISOR_HEALTH_STALE_MINUTES = 720

#: The per-tick ceiling: the sweep is the standing cost of watching
DEFAULT_SUPERVISOR_HEALTH_MAX_UNITS_PER_TICK = 4

#: Every armed probe rides in ONE system prompt, so both of these bound that prompt
DEFAULT_SUPERVISOR_MAX_ENABLED_PROBES = 12
DEFAULT_SUPERVISOR_PROBE_PROMPT_CHARS = 800


@dataclass(frozen=True)
class RemedyConfig:
    """What the supervisor is permitted to PROPOSE doing about a unit it judged ill (docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md §5.)"""

    enabled: bool = False
    allowed: tuple[str, ...] = ()


#: How many samples a median in the evolution report needs before it is reported as one (§9 of... (docs/superpowers/specs/2026-09-27-self-evolution.md)
DEFAULT_EVOLUTION_MIN_SAMPLES = 3

#: The steward's ledger (§3.3 of... (docs/superpowers/specs/2026-10-21-a-steward-decides-what-the-os-may-spend-on-itself.md)
DEFAULT_EVOLUTION_WEEKLY_USD = 150.0
DEFAULT_EVOLUTION_CRITICAL_WEEKLY_USD = 60.0
DEFAULT_EVOLUTION_YIELD_ABOVE_USD = 900.0
DEFAULT_EVOLUTION_ORDER_BUDGET_USD = 25.0
DEFAULT_EVOLUTION_MAX_DEPTH = 1
DEFAULT_EVOLUTION_MAJOR_MIN_REFS = 1
DEFAULT_EVOLUTION_MEDIUM_MIN_REFS = 3
#: Per origin kind, in place of the severity's `*_min_refs`: Neo raises a cleanup once, in one verdict
DEFAULT_EVOLUTION_MIN_REFS_BY_KIND = {"neo_rider": 1}
DEFAULT_EVOLUTION_MIN_PRECISION = 0.2
#: `shadow` decides and records but changes no dispatch; `enforce` applies the decision
EVOLUTION_MODES = ("shadow", "enforce")


@dataclass
class EvolutionConfig:
    """What the §9 evolution report needs before it believes its own numbers.... (docs/superpowers/specs/2026-09-27-self-evolution.md)"""

    min_samples: int = DEFAULT_EVOLUTION_MIN_SAMPLES
    # The steward (docs/superpowers/specs/2026-10-21-a-steward-decides-what-the-os-may-spend-on-itself.md...
    weekly_usd: float = DEFAULT_EVOLUTION_WEEKLY_USD
    critical_weekly_usd: float = DEFAULT_EVOLUTION_CRITICAL_WEEKLY_USD
    yield_above_usd: float = DEFAULT_EVOLUTION_YIELD_ABOVE_USD
    order_budget_usd: float = DEFAULT_EVOLUTION_ORDER_BUDGET_USD
    max_depth: int = DEFAULT_EVOLUTION_MAX_DEPTH
    major_min_refs: int = DEFAULT_EVOLUTION_MAJOR_MIN_REFS
    medium_min_refs: int = DEFAULT_EVOLUTION_MEDIUM_MIN_REFS
    min_refs_by_kind: dict[str, int] = field(
        default_factory=lambda: dict(DEFAULT_EVOLUTION_MIN_REFS_BY_KIND))
    min_precision: float = DEFAULT_EVOLUTION_MIN_PRECISION
    mode: str = "shadow"


@dataclass
class RulesConfig:
    """Whether the self-evolution registry is EVALUATED at all. §5.2 of... (docs/superpowers/specs/2026-09-27-self-evolution.md)"""

    enabled: bool = False
    #: How many person-marked false positives an ARMED detector may collect before it returns to `dry_run`...
    false_positive_disarm: int = 2


@dataclass
class SupervisorConfig:
    """The agent that reviews a cost alarm and either acks it or wants Neo — §2"""

    enabled: bool = False
    model: str = DEFAULT_SUPERVISOR_MODEL
    timeout: int = DEFAULT_SUPERVISOR_TIMEOUT
    learnings_limit: int = DEFAULT_SUPERVISOR_LEARNINGS_LIMIT
    max_age_hours: int = DEFAULT_SUPERVISOR_MAX_AGE_HOURS
    stale_reviewing_seconds: int = DEFAULT_SUPERVISOR_STALE_REVIEWING_SECONDS
    max_review_attempts: int = DEFAULT_SUPERVISOR_MAX_REVIEW_ATTEMPTS
    evidence_budget_chars: int = DEFAULT_SUPERVISOR_EVIDENCE_BUDGET_CHARS
    conversation_quote_chars: int = DEFAULT_SUPERVISOR_CONVERSATION_QUOTE_CHARS
    quoted_turns: int = DEFAULT_SUPERVISOR_QUOTED_TURNS
    description_chars: int = DEFAULT_SUPERVISOR_DESCRIPTION_CHARS
    note_chars: int = DEFAULT_SUPERVISOR_NOTE_CHARS
    reason_chars: int = DEFAULT_SUPERVISOR_REASON_CHARS

    # The health sweep
    probes: tuple[probes_mod.HealthProbe, ...] = probes_mod.DEFAULT_PROBES
    health_enabled: bool = False
    health_every_ticks: int = DEFAULT_SUPERVISOR_HEALTH_EVERY_TICKS
    health_min_interval_minutes: int = DEFAULT_SUPERVISOR_HEALTH_MIN_INTERVAL_MINUTES
    health_stale_minutes: int = DEFAULT_SUPERVISOR_HEALTH_STALE_MINUTES
    health_max_units_per_tick: int = DEFAULT_SUPERVISOR_HEALTH_MAX_UNITS_PER_TICK
    # Which re-derivable reasons count as explaining a still unit
    health_reassert_blockers: tuple[str, ...] = health_mod.BLOCKERS
    max_enabled_probes: int = DEFAULT_SUPERVISOR_MAX_ENABLED_PROBES
    probe_prompt_chars: int = DEFAULT_SUPERVISOR_PROBE_PROMPT_CHARS

    # What it may PROPOSE doing about what it finds (§5)
    remedies: RemedyConfig = field(default_factory=RemedyConfig)


@dataclass
class ProjectSpec:
    name: str
    path: Path
    description: str = ""
    model: str | None = None
    worker: WorkerDefaults = field(default_factory=WorkerDefaults)
    settings_overrides: dict[str, Any] = field(default_factory=dict)
    max_concurrent: int = DEFAULT_MAX_CONCURRENT
    # Privileged actions this project's workers may attempt under review rather than not at all (see...
    gates: GateConfig = field(default_factory=GateConfig)
    # Already RESOLVED against `os.validation`: `_parse_validation` is handed the OS config as its base (docs/superpowers/specs/2026-08-27-the-config-console.md §1.2.)
    validation: ValidationConfig = field(default_factory=ValidationConfig)
    inspect: InspectConfig = field(default_factory=InspectConfig)
    ui_health: UiHealthConfig = field(default_factory=UiHealthConfig)
    fleet_health: FleetHealthConfig = field(default_factory=FleetHealthConfig)
    navigation: NavigationConfig = field(default_factory=NavigationConfig)
    cost: CostConfig = field(default_factory=CostConfig)
    observability: ObservabilityConfig = field(default_factory=ObservabilityConfig)
    concision: ConcisionConfig = field(default_factory=ConcisionConfig)
    supervisor: SupervisorConfig = field(default_factory=SupervisorConfig)
    rules: RulesConfig = field(default_factory=RulesConfig)
    evolution: EvolutionConfig = field(default_factory=EvolutionConfig)
    messaging: MessagingConfig = field(default_factory=MessagingConfig)
    pull_requests: PullRequestsConfig = field(default_factory=PullRequestsConfig)
    bugs: BugsConfig = field(default_factory=BugsConfig)
    release: ReleaseConfig = field(default_factory=ReleaseConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    wiring: WiringConfig = field(default_factory=WiringConfig)
    worktree: WorktreeConfig = field(default_factory=WorktreeConfig)
    raw: dict[str, Any] = field(default_factory=dict)


# The seats the panel runs by default
DEFAULT_ROSTER = ("premise", "chair")

# Which question kinds the panel may answer by default
DEFAULT_PANEL_KINDS = ("question", "approval")

# Per-seat call timeout
DEFAULT_PANEL_TIMEOUT = 120


@dataclass
class PanelConfig:
    """Neo answering as a panel of profiled seats instead of as one agent"""

    enabled: bool = False
    roster: tuple[str, ...] = DEFAULT_ROSTER
    seat_models: dict[str, str] = field(default_factory=dict)
    chair_model: str = ""
    timeout: int = DEFAULT_PANEL_TIMEOUT
    kinds: tuple[str, ...] = DEFAULT_PANEL_KINDS
    fast_path: bool = True


@dataclass
class NeoConfig:
    """Neo, the OS answerer agent (responds to worker questions as the user)"""
    enabled: bool = True
    model: str = "opus"
    learnings_limit: int = 50
    # NOTE: parsed and never read — Neo's calls take `run_headless`'s own 300s default
    timeout: int = 300
    # Which model shortens an over-long question for the dashboard (`jarvis.digest`)
    digest_model: str = "haiku"
    #: How much of a question the digest model is shown, clipped and LABELLED (docs/superpowers/specs/2026-09-26-bounded-model-inputs.md)
    digest_max_question_chars: int = DEFAULT_DIGEST_MAX_QUESTION_CHARS
    #: Neo decides each improvement-order finding for the user; the steward decides what it files
    review_findings: bool = True
    panel: PanelConfig = field(default_factory=PanelConfig)


#: The answers `os.morpheus.on_failed` / `on_timeout` may give
MORPHEUS_ACTIONS = ("investigate", "neo")


@dataclass
class MorpheusConfig:
    """Morpheus, the production validator (docs/superpowers/specs/2026-10-21-morpheus-validates-releases-in-production.md)"""

    enabled: bool = True
    model: str = "sonnet"
    timeout_days: float = 3.0
    check_every_minutes: int = 15
    script_timeout_seconds: int = 60
    #: Compact Morpheus's session once its context passes this share of the window
    compact_at: float = 0.30
    on_failed: str = "investigate"
    on_timeout: str = "neo"
    turn_timeout_seconds: int = 900


def _parse_morpheus(raw: Any, where: str = "os.morpheus") -> MorpheusConfig:
    """`os.morpheus`, field-level over the defaults"""
    base = MorpheusConfig()
    if not isinstance(raw, dict):
        raise _err(f'"{where}" must be an object')
    enabled = raw.get("enabled", base.enabled)
    if not isinstance(enabled, bool):
        raise _err(f"{where}.enabled must be true or false, got {enabled!r}")
    try:
        cfg = MorpheusConfig(
            enabled=enabled,
            model=str(raw.get("model", base.model) or base.model),
            timeout_days=float(raw.get("timeout_days", base.timeout_days)),
            check_every_minutes=int(raw.get("check_every_minutes",
                                            base.check_every_minutes)),
            script_timeout_seconds=int(raw.get("script_timeout_seconds",
                                               base.script_timeout_seconds)),
            compact_at=float(raw.get("compact_at", base.compact_at)),
            on_failed=str(raw.get("on_failed", base.on_failed)),
            on_timeout=str(raw.get("on_timeout", base.on_timeout)),
            turn_timeout_seconds=int(raw.get("turn_timeout_seconds",
                                             base.turn_timeout_seconds)),
        )
    except (TypeError, ValueError) as e:
        raise _err(f"{where}: {e}") from e
    if cfg.timeout_days <= 0:
        raise _err(f"{where}.timeout_days must be > 0")
    for name in ("check_every_minutes", "script_timeout_seconds", "turn_timeout_seconds"):
        if getattr(cfg, name) < 1:
            raise _err(f"{where}.{name} must be >= 1")
    if not 0.0 < cfg.compact_at < 1.0:
        raise _err(f"{where}.compact_at must be a share of the window between 0 and 1, "
                   f"got {cfg.compact_at}")
    for name in ("on_failed", "on_timeout"):
        if getattr(cfg, name) not in MORPHEUS_ACTIONS:
            raise _err(f"{where}.{name} must be one of {list(MORPHEUS_ACTIONS)}, "
                       f"got {getattr(cfg, name)!r}")
    return cfg


@dataclass
class OsConfig:
    default_model: str = DEFAULT_MODEL
    default_effort: str | None = None
    default_permission_mode: str = DEFAULT_PERMISSION_MODE
    # Spec 2026-10-01-the-steer-that-beat-the-brief.md §1 — see VALID_BASH_FIRST
    default_bash_first: str = DEFAULT_WORKER_BASH_FIRST
    # Spec 2026-10-02-serena-the-cheap-path.md §4 — see VALID_TOOL_SEARCH
    default_tool_search: str = DEFAULT_WORKER_TOOL_SEARCH
    # Spec 2026-10-15-the-hour-on-worker-turns-only.md §1 — see VALID_WORKER_CACHE_TTL
    default_cache_ttl: str = DEFAULT_WORKER_CACHE_TTL
    # Spec 2026-10-22-workers-on-background-sessions.md — see VALID_WORKER_TRANSPORT
    default_transport: str = DEFAULT_WORKER_TRANSPORT
    # Spec 2026-10-02-serena-the-cheap-path.md §6 — see VALID_PY_NAV_HOOK
    default_py_nav_hook: str = DEFAULT_WORKER_PY_NAV_HOOK
    # Spec 2026-10-06-navigate-specs-like-code.md §5 — see VALID_DOC_NAV_HOOK
    default_doc_nav_hook: str = DEFAULT_WORKER_DOC_NAV_HOOK
    # Its bar, fleet-wide — see DEFAULT_WORKER_DOC_READ_LIMIT_LINES
    default_doc_read_limit_lines: int = DEFAULT_WORKER_DOC_READ_LIMIT_LINES
    default_max_concurrent: int = DEFAULT_MAX_CONCURRENT
    #: Fleet-wide worker turns in flight
    max_in_flight: int = DEFAULT_MAX_IN_FLIGHT
    default_autocompact_window: int | None = DEFAULT_AUTOCOMPACT_WINDOW
    default_budget_usd: float | None = DEFAULT_BUDGET_USD
    default_feature_budget_usd: float | None = DEFAULT_FEATURE_BUDGET_USD
    #: Read by the cost surfaces, not by a worker launch — see DEFAULT_COLD_PREFIX_FLOOR
    cold_prefix_floor: int = DEFAULT_COLD_PREFIX_FLOOR
    cold_prefix_floor_max: int = DEFAULT_COLD_PREFIX_FLOOR_MAX
    #: Read by the two `jarvis doctor` cache-health post-conditions
    cache_health_window_days: int = DEFAULT_CACHE_HEALTH_WINDOW_DAYS
    cache_health_min_orders: int = DEFAULT_CACHE_HEALTH_MIN_ORDERS
    cache_health_min_boundaries: int = DEFAULT_CACHE_HEALTH_MIN_BOUNDARIES
    cache_health_prefix_share: float = DEFAULT_CACHE_HEALTH_PREFIX_SHARE
    #: The smallest context the OS will compact past an expired cache, or None to never compact
    compact_min_context: int | None = DEFAULT_COMPACT_MIN_CONTEXT
    #: Backstop ceiling on one OS-side model call's combined prompt
    max_os_prompt_chars: int = DEFAULT_MAX_OS_PROMPT_CHARS
    #: The floor the daemon warns under, before SQLite starts failing
    disk_free_warn_gb: float = DEFAULT_DISK_FREE_WARN_GB
    notification_sinks: list[str] = field(default_factory=lambda: ["log"])
    telegram_token_env: str = "JARVIS_TELEGRAM_TOKEN"
    telegram_chat_id_env: str = "JARVIS_TELEGRAM_CHAT_ID"
    ui_port: int = 8787
    # Where notification deep links point
    ui_base_url: str = ""
    # Knowledge reaches workers as an index they query on demand
    knowledge_inject_limit: int = 8      # max pinned entries injected verbatim
    knowledge_digest_limit: int = 8      # max index lines
    knowledge_digest_chars: int = 1000   # hard char budget for those lines
    knowledge_pinned_chars: int = 360    # per pinned entry; the rest is `jarvis learn show`
    # Entries matching the work order's own TITLE, surfaced as a pointer into the overflow
    knowledge_hint_limit: int = 3         # max title-matched hint lines
    knowledge_hint_chars: int = 400       # hard char budget for those lines
    neo: NeoConfig = field(default_factory=NeoConfig)
    validation: ValidationConfig = field(default_factory=ValidationConfig)
    inspect: InspectConfig = field(default_factory=InspectConfig)
    ui_health: UiHealthConfig = field(default_factory=UiHealthConfig)
    fleet_health: FleetHealthConfig = field(default_factory=FleetHealthConfig)
    navigation: NavigationConfig = field(default_factory=NavigationConfig)
    cost: CostConfig = field(default_factory=CostConfig)
    observability: ObservabilityConfig = field(default_factory=ObservabilityConfig)
    concision: ConcisionConfig = field(default_factory=ConcisionConfig)
    supervisor: SupervisorConfig = field(default_factory=SupervisorConfig)
    rules: RulesConfig = field(default_factory=RulesConfig)
    evolution: EvolutionConfig = field(default_factory=EvolutionConfig)
    messaging: MessagingConfig = field(default_factory=MessagingConfig)
    pull_requests: PullRequestsConfig = field(default_factory=PullRequestsConfig)
    bugs: BugsConfig = field(default_factory=BugsConfig)
    release: ReleaseConfig = field(default_factory=ReleaseConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    wiring: WiringConfig = field(default_factory=WiringConfig)
    worktree: WorktreeConfig = field(default_factory=WorktreeConfig)
    autonomy: AutonomyConfig = field(default_factory=AutonomyConfig)
    morpheus: MorpheusConfig = field(default_factory=MorpheusConfig)


@dataclass
class Catalog:
    os: OsConfig
    projects: list[ProjectSpec]
    source_path: Path | None = None

    def project(self, name: str) -> ProjectSpec:
        for p in self.projects:
            if p.name == name:
                return p
        raise CatalogError(f"unknown project {name!r} (known: {[p.name for p in self.projects]})")

    def without_projects(self, names: Collection[str]) -> "Catalog":
        """A copy with `names` dropped; the caller says which (docs/superpowers/specs/2026-10-28-offboard-a-project.md §6.1)"""
        return dataclasses.replace(
            self, projects=[p for p in self.projects if p.name not in names])


def _err(msg: str) -> CatalogError:
    return CatalogError(f"catalog error: {msg}")


def _positive_int_or_err(os_raw: dict[str, Any], key: str, default: int) -> int:
    value = os_raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _err(f"os.{key} must be an integer")
    return value


def _cold_prefix_floor_or_err(os_raw: dict[str, Any]) -> tuple[int, int]:
    """`os.cold_prefix_floor` and its guard rail, validated at boot not at report time"""
    ceiling = _positive_int_or_err(os_raw, "cold_prefix_floor_max",
                                   DEFAULT_COLD_PREFIX_FLOOR_MAX)
    if ceiling <= 0:
        raise _err(f"os.cold_prefix_floor_max {ceiling} must be positive")
    floor = _positive_int_or_err(os_raw, "cold_prefix_floor",
                                 DEFAULT_COLD_PREFIX_FLOOR)
    if not 0 <= floor <= ceiling:
        raise _err(f"os.cold_prefix_floor {floor} out of range 0-{ceiling} "
                   f"(os.cold_prefix_floor_max)")
    return floor, ceiling


def _compact_min_context_or_err(os_raw: dict[str, Any]) -> int | None:
    """`os.compact_min_context`, validated at boot"""
    value = os_raw.get("compact_min_context", _MISSING)
    if value is _MISSING:
        return DEFAULT_COMPACT_MIN_CONTEXT
    if value is None or value is False:
        return None
    if isinstance(value, bool) or not isinstance(value, int):
        raise _err(f"os.compact_min_context must be a whole number of tokens or null, "
                   f"got {value!r}")
    if value < COMPACT_MIN_CONTEXT_MIN:
        raise _err(f"os.compact_min_context {value} is below "
                   f"{COMPACT_MIN_CONTEXT_MIN}, where compacting costs more than the "
                   f"re-write it replaces (use null to switch compaction off)")
    return value


def _disk_free_warn_gb_or_err(os_raw: dict[str, Any]) -> float:
    """`os.disk_free_warn_gb`, validated at boot — 0 is the off switch"""
    value = os_raw.get("disk_free_warn_gb", DEFAULT_DISK_FREE_WARN_GB)
    if isinstance(value, bool) or not isinstance(value, (int, float)) or value < 0:
        raise _err(f"os.disk_free_warn_gb must be a non-negative number of gigabytes "
                   f"(0 to never warn), got {value!r}")
    return float(value)


def _max_os_prompt_chars_or_err(os_raw: dict[str, Any]) -> int:
    """`os.max_os_prompt_chars`, validated at boot"""
    value = os_raw.get("max_os_prompt_chars", DEFAULT_MAX_OS_PROMPT_CHARS)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _err(f"os.max_os_prompt_chars must be a whole number of characters, "
                   f"got {value!r} (there is deliberately no off switch)")
    if value < MAX_OS_PROMPT_CHARS_MIN:
        raise _err(f"os.max_os_prompt_chars {value} is below "
                   f"{MAX_OS_PROMPT_CHARS_MIN}, where it would refuse the validation "
                   f"panel's own prompts and silently disable validation")
    return value


def _digest_max_question_chars_or_err(neo_raw: dict[str, Any]) -> int:
    """`os.neo.digest_max_question_chars` — see DEFAULT_DIGEST_MAX_QUESTION_CHARS (docs/superpowers/specs/2026-09-26-bounded-model-inputs.md)"""
    from .digest import MIN_CHARS

    value = neo_raw.get("digest_max_question_chars", DEFAULT_DIGEST_MAX_QUESTION_CHARS)
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise _err("os.neo.digest_max_question_chars must be a positive whole number "
                   f"of characters, got {value!r}")
    if value < MIN_CHARS:
        raise _err(f"os.neo.digest_max_question_chars {value} is below {MIN_CHARS}, "
                   f"the length at which a question is digested at all "
                   f"(`digest.MIN_CHARS`): under it every digested question would be "
                   f"clipped to less than the threshold that earned it the call")
    return value


def _cache_health_or_err(os_raw: dict[str, Any]) -> tuple[int, int, int, float]:
    """The cache-health window and its three floors, validated at boot"""
    days = _positive_int_or_err(os_raw, "cache_health_window_days",
                                DEFAULT_CACHE_HEALTH_WINDOW_DAYS)
    if days < 1:
        raise _err(f"os.cache_health_window_days {days} must be >= 1")
    min_orders = _positive_int_or_err(os_raw, "cache_health_min_orders",
                                      DEFAULT_CACHE_HEALTH_MIN_ORDERS)
    if min_orders < 1:
        raise _err(f"os.cache_health_min_orders {min_orders} must be >= 1")
    min_boundaries = _positive_int_or_err(os_raw, "cache_health_min_boundaries",
                                          DEFAULT_CACHE_HEALTH_MIN_BOUNDARIES)
    if min_boundaries < 1:
        raise _err(f"os.cache_health_min_boundaries {min_boundaries} must be >= 1")
    share = os_raw.get("cache_health_prefix_share", DEFAULT_CACHE_HEALTH_PREFIX_SHARE)
    if isinstance(share, bool) or not isinstance(share, (int, float)):
        raise _err("os.cache_health_prefix_share must be a number")
    if not 0.0 < float(share) <= 1.0:
        raise _err(f"os.cache_health_prefix_share {share} out of range 0-1 "
                   f"(a share of all cache writes, not a percentage)")
    return days, min_orders, min_boundaries, float(share)


def _autocompact_or_err(raw: dict[str, Any], key: str, where: str,
                        default: int | None) -> int | None:
    try:
        return _parse_autocompact(raw, key, where, default)
    except ValueError as e:
        raise _err(str(e)) from e


def _doc_read_limit_or_err(raw: dict[str, Any], where: str, default: int) -> int:
    """`worker.doc_read_limit_lines`: an int ABOVE ZERO, and the message names the key (docs/superpowers/specs/2026-10-06-navigate-specs-like-code.md)"""
    value = raw.get("doc_read_limit_lines", default)
    try:
        lines = int(value)
    except (TypeError, ValueError) as e:
        raise _err(f"{where} must be a whole number of lines, got {value!r}") from e
    if lines < WORKER_DOC_READ_LIMIT_LINES_MIN:
        raise _err(f"{where} must be >= {WORKER_DOC_READ_LIMIT_LINES_MIN} (lines — zero "
                   f"or less would refuse every targeted read), got {lines}")
    return lines


def load_catalog(path: str | Path) -> Catalog:
    path = Path(path).expanduser()
    if not path.exists():
        raise _err(f"file not found: {path}")
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise _err(f"invalid JSON in {path}: {e}") from e
    cat = parse_catalog(data, source_path=path)
    # Arm the transport backstop here: every startup path (docs/superpowers/specs/2026-09-26-bounded-model-inputs.md)
    claude_cli.set_max_os_prompt_chars(cat.os.max_os_prompt_chars)
    return cat


def _parse_panel(raw: Any) -> PanelConfig:
    """`os.neo.panel`, validated against the vocabularies it names"""
    if not isinstance(raw, dict):
        raise _err('"os.neo.panel" must be an object')
    roster = tuple(raw.get("roster", DEFAULT_ROSTER))
    unknown = [s for s in roster if s not in SEATS]
    if unknown:
        raise _err(f"os.neo.panel.roster names unknown seat(s) {unknown} "
                   f"(known: {list(SEATS)})")
    seat_models = raw.get("seat_models", {}) or {}
    if not isinstance(seat_models, dict):
        raise _err('"os.neo.panel.seat_models" must be an object')
    unknown = [s for s in seat_models if s not in SEATS]
    if unknown:
        raise _err(f"os.neo.panel.seat_models names unknown seat(s) {unknown} "
                   f"(known: {list(SEATS)})")
    kinds = tuple(raw.get("kinds", DEFAULT_PANEL_KINDS))
    unknown = [k for k in kinds if k not in Q_KINDS]
    if unknown:
        raise _err(f"os.neo.panel.kinds names unknown question kind(s) {unknown} "
                   f"(known: {list(Q_KINDS)})")
    timeout = int(raw.get("timeout", DEFAULT_PANEL_TIMEOUT))
    if timeout < 1:
        raise _err("os.neo.panel.timeout must be >= 1")
    return PanelConfig(
        enabled=bool(raw.get("enabled", False)),
        roster=roster,
        seat_models={k: str(v) for k, v in seat_models.items()},
        chair_model=str(raw.get("chair_model", "") or ""),
        timeout=timeout,
        kinds=kinds,
        fast_path=bool(raw.get("fast_path", True)),
    )


def _parse_validation(raw: Any, base: ValidationConfig | None = None,
                      where: str = "os.validation") -> ValidationConfig:
    """`os.validation` — or a project's override of it — against the seat vocabulary"""
    base = base or ValidationConfig()
    if not isinstance(raw, dict):
        raise _err(f'"{where}" must be an object')
    roster = tuple(raw.get("roster", base.roster))
    unknown = [s for s in roster if s not in VALIDATOR_SEATS]
    if unknown:
        raise _err(f"{where}.roster names unknown seat(s) {unknown} "
                   f"(known: {list(VALIDATOR_SEATS)})")
    seat_models = raw.get("seat_models", base.seat_models) or {}
    if not isinstance(seat_models, dict):
        raise _err(f'"{where}.seat_models" must be an object')
    unknown = [s for s in seat_models if s not in VALIDATOR_SEATS]
    if unknown:
        raise _err(f"{where}.seat_models names unknown seat(s) {unknown} "
                   f"(known: {list(VALIDATOR_SEATS)})")
    timeout = int(raw.get("timeout", base.timeout))
    if timeout < 1:
        raise _err(f"{where}.timeout must be >= 1")
    max_rounds = int(raw.get("max_rounds", base.max_rounds))
    if max_rounds < 1:
        raise _err(f"{where}.max_rounds must be >= 1")
    red_ci_returns = int(raw.get("red_ci_returns", base.red_ci_returns))
    if red_ci_returns < 0:
        # 0 is legal and means "never return": send every red head to the seats
        raise _err(f"{where}.red_ci_returns must be >= 0")
    killed_retries = int(raw.get("killed_retries", base.killed_retries))
    if killed_retries < 0:
        # 0 is legal and means "escalate on the first kill" (spec §4b)
        raise _err(f"{where}.killed_retries must be >= 0")
    diff_chars = int(raw.get("diff_chars", base.diff_chars))
    if diff_chars < 1:
        raise _err(f"{where}.diff_chars must be >= 1")
    decision_record_chars = int(
        raw.get("decision_record_chars", base.decision_record_chars))
    if decision_record_chars < 1:
        raise _err(f"{where}.decision_record_chars must be >= 1")
    assumption_spec_chars = int(
        raw.get("assumption_spec_chars", base.assumption_spec_chars))
    if assumption_spec_chars < 1:
        raise _err(f"{where}.assumption_spec_chars must be >= 1")
    stakes_classifier = str(
        raw.get("stakes_classifier", base.stakes_classifier) or DEFAULT_STAKES_CLASSIFIER)
    if stakes_classifier not in STAKES_CLASSIFIER_MODES:
        # The `roster` precedent: refused loudly rather than shrugged to the default
        raise _err(f"{where}.stakes_classifier is {stakes_classifier!r}, which is not "
                   f"one of {list(STAKES_CLASSIFIER_MODES)}")
    feature_merge_wait_minutes = int(
        raw.get("feature_merge_wait_minutes", base.feature_merge_wait_minutes))
    if feature_merge_wait_minutes < 0:
        # 0 is legal and means "never defer": check once, flag immediately
        raise _err(f"{where}.feature_merge_wait_minutes must be >= 0")
    withdrawal_rate = float(raw.get("objection_withdrawal_alarm_rate",
                                    base.objection_withdrawal_alarm_rate))
    if not 0.0 <= withdrawal_rate <= 1.0:
        # A share: 1 means never raise it, 0 means raise it on any withdrawal at all
        raise _err(f"{where}.objection_withdrawal_alarm_rate must be between 0 and 1")
    max_follow_ups = int(raw.get("max_follow_ups", base.max_follow_ups))
    if max_follow_ups < 0:
        # 0 is legal and is NOT the same setting as `follow_ups: false`: it files nothing while leaving every...
        raise _err(f"{where}.max_follow_ups must be >= 0")
    return ValidationConfig(
        enabled=bool(raw.get("enabled", base.enabled)),
        roster=roster,
        seat_models={k: str(v) for k, v in seat_models.items()},
        chair_model=str(raw.get("chair_model", base.chair_model) or ""),
        timeout=timeout,
        max_rounds=max_rounds,
        red_ci_returns=red_ci_returns,
        killed_retries=killed_retries,
        diff_chars=diff_chars,
        decision_record_chars=decision_record_chars,
        assumption_spec_chars=assumption_spec_chars,
        feature_units=bool(raw.get("feature_units", base.feature_units)),
        feature_merge_wait_minutes=feature_merge_wait_minutes,
        # Same field-level fallback as every flag in this block — see `auto_merge` below
        follow_ups=bool(raw.get("follow_ups", base.follow_ups)),
        max_follow_ups=max_follow_ups,
        # `base.auto_merge` is the fleet answer when this project names nothing
        auto_merge=bool(raw.get("auto_merge", base.auto_merge)),
        # Same fallback, same reason — see `ValidationConfig.auto_review`
        auto_review=bool(raw.get("auto_review", base.auto_review)),
        # Same fallback again — see `ValidationConfig.stakes_classifier`
        stakes_classifier=stakes_classifier,
        objection_withdrawal_alarm_rate=withdrawal_rate,
    )


def _parse_inspect(raw: Any, base: InspectConfig | None = None,
                   where: str = "os.inspect") -> InspectConfig:
    """`os.inspect`, or a project's override of it, with absurd values refused"""
    base = base or InspectConfig()
    if not isinstance(raw, dict):
        raise _err(f'"{where}" must be an object')
    cfg = InspectConfig(
        enabled=bool(raw.get("enabled", base.enabled)),
        report_write_floor=int(raw.get("report_write_floor",
                                       base.report_write_floor)),
        report_join_floor=int(raw.get("report_join_floor", base.report_join_floor)),
        quote_chars=int(raw.get("quote_chars", base.quote_chars)),
        alarm_turn_minutes=int(raw.get("alarm_turn_minutes",
                                       base.alarm_turn_minutes)),
        alarm_stalled_minutes=int(raw.get("alarm_stalled_minutes",
                                          base.alarm_stalled_minutes)),
        alarm_awaiting_minutes=int(raw.get("alarm_awaiting_minutes",
                                           base.alarm_awaiting_minutes)),
        alarm_join_seconds=int(raw.get("alarm_join_seconds",
                                       base.alarm_join_seconds)),
        alarm_subagent_tool_minutes=int(raw.get("alarm_subagent_tool_minutes",
                                                base.alarm_subagent_tool_minutes)),
        alarm_write_tokens=int(raw.get("alarm_write_tokens",
                                       base.alarm_write_tokens)),
        alarm_parked_minutes=int(raw.get("alarm_parked_minutes",
                                         base.alarm_parked_minutes)),
        alarm_compaction_window_days=int(raw.get("alarm_compaction_window_days",
                                                 base.alarm_compaction_window_days)),
        alarm_rewrite_window_days=int(raw.get("alarm_rewrite_window_days",
                                              base.alarm_rewrite_window_days)),
        alarm_rewrite_prefix_share=float(raw.get("alarm_rewrite_prefix_share",
                                                 base.alarm_rewrite_prefix_share)),
        alarm_rewrite_ttl_share=float(raw.get("alarm_rewrite_ttl_share",
                                              base.alarm_rewrite_ttl_share)),
        alarm_rewrite_min_orders=int(raw.get("alarm_rewrite_min_orders",
                                             base.alarm_rewrite_min_orders)),
        alarm_rewrite_min_usd=float(raw.get("alarm_rewrite_min_usd",
                                            base.alarm_rewrite_min_usd)),
        alarm_cache_1h_window_days=int(raw.get("alarm_cache_1h_window_days",
                                               base.alarm_cache_1h_window_days)),
        alarm_cache_1h_dispatched_tokens=int(
            raw.get("alarm_cache_1h_dispatched_tokens",
                    base.alarm_cache_1h_dispatched_tokens)),
        alarm_cache_arm_missed_tokens=int(
            raw.get("alarm_cache_arm_missed_tokens",
                    base.alarm_cache_arm_missed_tokens)),
    )
    for name, value in vars(cfg).items():
        if name in INSPECT_FRACTION_KEYS:
            if not 0 < value <= 1:
                raise _err(f"{where}.{name} must be a fraction in (0, 1] — {value} is "
                           f"a share of the project's bill, and above 1 it is not a "
                           f"strict threshold but one that can never fire")
        elif name in INSPECT_MONEY_KEYS:
            if value < 0:
                raise _err(f"{where}.{name} must be >= 0")
        elif name != "enabled" and value < 1:
            raise _err(f"{where}.{name} must be >= 1")
    return cfg


def _parse_ui_health(raw: Any, base: UiHealthConfig | None = None,
                     where: str = "os.ui_health") -> UiHealthConfig:
    """`os.ui_health`, or a project's override of it — `_parse_inspect`'s shape exactly"""
    base = base or UiHealthConfig()
    if not isinstance(raw, dict):
        raise _err(f'"{where}" must be an object')
    cfg = UiHealthConfig(
        enabled=bool(raw.get("enabled", base.enabled)),
        probe_interval_seconds=int(raw.get("probe_interval_seconds",
                                           base.probe_interval_seconds)),
        probe_timeout_seconds=int(raw.get("probe_timeout_seconds",
                                          base.probe_timeout_seconds)),
        trip_threshold=int(raw.get("trip_threshold", base.trip_threshold)),
        healthz_timeout_seconds=int(raw.get("healthz_timeout_seconds",
                                            base.healthz_timeout_seconds)),
        restart_cooldown_seconds=int(raw.get("restart_cooldown_seconds",
                                             base.restart_cooldown_seconds)),
        max_restarts_per_day=int(raw.get("max_restarts_per_day",
                                         base.max_restarts_per_day)),
        boot_grace_seconds=int(raw.get("boot_grace_seconds",
                                       base.boot_grace_seconds)),
        loop_stall_seconds=int(raw.get("loop_stall_seconds",
                                       base.loop_stall_seconds)),
    )
    for name, value in vars(cfg).items():
        if name != "enabled" and value < 1:
            raise _err(f"{where}.{name} must be >= 1")
    return cfg


def _parse_fleet_health(raw: Any, base: FleetHealthConfig | None = None,
                        where: str = "os.fleet_health") -> FleetHealthConfig:
    """`os.fleet_health`, or a project's override of it — `_parse_inspect`'s shape"""
    from .project_store import OPEN_STATUSES, WO_KINDS

    base = base or FleetHealthConfig()
    if not isinstance(raw, dict):
        raise _err(f'"{where}" must be an object')
    if where != "os.fleet_health":
        for key, why in FLEET_HEALTH_FLEET_ONLY_KEYS.items():
            if key in raw:
                raise _err(f"{where}.{key} is a FLEET number — set "
                           f"os.fleet_health.{key}. {why}")
    thresholds = dict(base.thresholds)
    named = raw.get("thresholds", {})
    if not isinstance(named, dict):
        raise _err(f'"{where}.thresholds" must be an object')
    for status, minutes in named.items():
        if status not in OPEN_STATUSES:
            raise _err(f"{where}.thresholds: {status!r} is not an open status "
                       f"(known: {sorted(OPEN_STATUSES)})")
        if int(minutes) < 1:
            raise _err(f"{where}.thresholds.{status} must be >= 1")
        thresholds[status] = int(minutes)
    # A WHOLE-FIELD override
    exempt_kinds = base.exempt_kinds
    if "exempt_kinds" in raw:
        named_kinds = raw["exempt_kinds"]
        if not isinstance(named_kinds, list) \
                or not all(isinstance(k, str) for k in named_kinds):
            raise _err(f'"{where}.exempt_kinds" must be a list of strings')
        for k in named_kinds:
            # Refused rather than ignored: a typo that silently exempted nothing is how a manager stays watched...
            if k not in WO_KINDS:
                raise _err(f"{where}.exempt_kinds: {k!r} is not a work-order kind "
                           f"(known: {sorted(WO_KINDS)})")
        exempt_kinds = tuple(named_kinds)
    cfg = FleetHealthConfig(
        enabled=bool(raw.get("enabled", base.enabled)),
        thresholds=thresholds,
        fallback_minutes=int(raw.get("fallback_minutes", base.fallback_minutes)),
        cooldown_minutes=int(raw.get("cooldown_minutes", base.cooldown_minutes)),
        max_per_day=int(raw.get("max_per_day", base.max_per_day)),
        sweep_every_ticks=int(raw.get("sweep_every_ticks", base.sweep_every_ticks)),
        sweep_dark_minutes=int(raw.get("sweep_dark_minutes", base.sweep_dark_minutes)),
        exempt_kinds=exempt_kinds,
        red_episode_retention_hours=int(raw.get(
            "red_episode_retention_hours", base.red_episode_retention_hours)),
        base_fix_grace_minutes=int(raw.get(
            "base_fix_grace_minutes", base.base_fix_grace_minutes)),
    )
    for name, value in vars(cfg).items():
        if name in FLEET_HEALTH_MAP_KEYS or name in FLEET_HEALTH_LIST_KEYS \
                or name == "enabled":
            continue
        if value < 1:
            raise _err(f"{where}.{name} must be >= 1")
    return cfg


def _parse_navigation(raw: Any, base: NavigationConfig | None = None,
                      where: str = "os.navigation") -> NavigationConfig:
    """`os.navigation`, or a project's override of it"""
    base = base or NavigationConfig()
    if not isinstance(raw, dict):
        raise _err(f'"{where}" must be an object')

    def patterns(name: str, fallback: tuple[str, ...]) -> tuple[str, ...]:
        if name not in raw:
            return fallback
        value = raw[name]
        if (not isinstance(value, (list, tuple))
                or not all(isinstance(v, str) for v in value)):
            raise _err(f'"{where}.{name}" must be a list of strings')
        if not value:
            raise _err(f'"{where}.{name}" must not be empty — an empty classifier '
                       f"reports 0% everywhere and looks like a win")
        return tuple(value)

    cfg = NavigationConfig(
        enabled=bool(raw.get("enabled", base.enabled)),
        bash_commands=patterns("bash_commands", base.bash_commands),
        symbol_tools=patterns("symbol_tools", base.symbol_tools),
        text_search_tools=patterns("text_search_tools", base.text_search_tools),
        code_suffixes=patterns("code_suffixes", base.code_suffixes),
        # docs/superpowers/specs/2026-10-06-navigate-specs-like-code.md §3.2
        doc_suffixes=patterns("doc_suffixes", base.doc_suffixes),
        # docs/superpowers/specs/2026-10-13-a-confirmation-question-with-a-zero-character- diff.md: the...
        doc_dirs=frozenset(patterns("doc_dirs", tuple(base.doc_dirs))),
        doc_dump_commands=patterns("doc_dump_commands", base.doc_dump_commands),
        window_days=int(raw.get("window_days", base.window_days)),
    )
    if cfg.window_days < 1:
        raise _err(f"{where}.window_days must be >= 1")
    for name in ("code_suffixes", "doc_suffixes"):
        for suffix in getattr(cfg, name):
            if not suffix.startswith("."):
                raise _err(f'"{where}.{name}" entries must start with a dot — '
                           f"{suffix!r} matches no path")
    return cfg


def _parse_cost(raw: Any, base: CostConfig | None = None,
                where: str = "os.cost") -> CostConfig:
    """`os.cost`, or a project's override of it, with absurd values refused (docs/superpowers/specs/2026-10-06-fleet-cost-distribution.md)"""
    base = base or CostConfig()
    if not isinstance(raw, dict):
        raise _err(f'"{where}" must be an object')
    cfg = CostConfig(
        week_reset_weekday=int(raw.get("week_reset_weekday", base.week_reset_weekday)),
        week_reset_hour=int(raw.get("week_reset_hour", base.week_reset_hour)),
        week_reset_zone=str(raw.get("week_reset_zone", base.week_reset_zone)),
        percentile=float(raw.get("percentile", base.percentile)),
        max_orders=int(raw.get("max_orders", base.max_orders)),
        chars_per_token=float(raw.get("chars_per_token", base.chars_per_token)),
        tool_rows=int(raw.get("tool_rows", base.tool_rows)),
        session_window_hours=float(raw.get("session_window_hours",
                                           base.session_window_hours)),
        meter_nearest_seconds=int(raw.get("meter_nearest_seconds",
                                          base.meter_nearest_seconds)),
        meter_calibration_min_minutes=int(raw.get(
            "meter_calibration_min_minutes", base.meter_calibration_min_minutes)),
        meter_calibration_min_points=float(raw.get(
            "meter_calibration_min_points", base.meter_calibration_min_points)),
        meter_calibration_days=int(raw.get("meter_calibration_days",
                                           base.meter_calibration_days)),
        meter_dollars_per_point=float(raw.get("meter_dollars_per_point",
                                              base.meter_dollars_per_point)),
        meter_stale_minutes=int(raw.get("meter_stale_minutes",
                                        base.meter_stale_minutes)),
        meter_gap_notice_samples=int(raw.get("meter_gap_notice_samples",
                                             base.meter_gap_notice_samples)),
        meter_outside_rows=int(raw.get("meter_outside_rows", base.meter_outside_rows)),
        meter_outside_alert_share=float(raw.get("meter_outside_alert_share",
                                                base.meter_outside_alert_share)),
        meter_residual_alert_share=float(raw.get("meter_residual_alert_share",
                                                 base.meter_residual_alert_share)),
        meter_alert_min_usd=float(raw.get("meter_alert_min_usd",
                                          base.meter_alert_min_usd)),
    )
    if not 0 <= cfg.week_reset_weekday <= 6:
        raise _err(f"{where}.week_reset_weekday must be 0..6 (0 = Monday) — "
                   f"{cfg.week_reset_weekday} is not a day of the week")
    if not 0 <= cfg.week_reset_hour <= 23:
        raise _err(f"{where}.week_reset_hour must be 0..23 — {cfg.week_reset_hour} is "
                   f"not an hour of the day")
    if not 0 < cfg.percentile < 1:
        raise _err(f"{where}.percentile must be strictly inside (0, 1) — "
                   f"{cfg.percentile} names either no observation or the maximum, which "
                   f"the report already reports beside it")
    if cfg.max_orders < 1:
        raise _err(f"{where}.max_orders must be >= 1")
    if cfg.chars_per_token <= 0:
        raise _err(f"{where}.chars_per_token must be > 0 — {cfg.chars_per_token} is not "
                   f"a divisor, and a negative one would report negative tokens")
    if cfg.tool_rows < 1:
        raise _err(f"{where}.tool_rows must be >= 1")
    # A LENGTH, not a count: 2.5 hours is a legal belief about the grid
    if cfg.session_window_hours <= 0:
        raise _err(f"{where}.session_window_hours must be > 0 — "
                   f"{cfg.session_window_hours} is not a length of time")
    # The meter's seven
    for name in ("meter_nearest_seconds", "meter_calibration_min_minutes",
                 "meter_calibration_min_points", "meter_calibration_days",
                 "meter_dollars_per_point"):
        if getattr(cfg, name) <= 0:
            raise _err(f"{where}.{name} must be > 0 — {getattr(cfg, name)} is not a "
                       f"length, a count of points or a price")
    for name in ("meter_stale_minutes", "meter_gap_notice_samples",
                 "meter_outside_rows"):
        if getattr(cfg, name) < 1:
            raise _err(f"{where}.{name} must be >= 1 — zero would fire on the first "
                       f"failed read, which is a blip and not an outage")
    # A SHARE of the implied spend, so strictly inside (0
    for name in ("meter_outside_alert_share", "meter_residual_alert_share"):
        if not 0 < getattr(cfg, name) <= 1:
            raise _err(f"{where}.{name} must be inside (0, 1] — {getattr(cfg, name)} "
                       f"is not a share of the implied spend")
    # A FLOOR in dollars, and zero is legal: it means every window is big enough
    if cfg.meter_alert_min_usd < 0:
        raise _err(f"{where}.meter_alert_min_usd must be >= 0 — "
                   f"{cfg.meter_alert_min_usd} is not an amount of money")
    try:
        zoneinfo.ZoneInfo(cfg.week_reset_zone)
    except (zoneinfo.ZoneInfoNotFoundError, ValueError) as e:
        raise _err(f"{where}.week_reset_zone {cfg.week_reset_zone!r} is not a time zone "
                   f"this system knows ({e}) — the usage week would start at the wrong "
                   f"instant every week") from e
    return cfg


def _parse_observability(raw: Any, base: ObservabilityConfig | None = None,
                        where: str = "os.observability") -> ObservabilityConfig:
    """`os.observability`, or a project's override of it"""
    base = base or ObservabilityConfig()
    if not isinstance(raw, dict):
        raise _err(f'"{where}" must be an object')
    level = str(raw.get("level", base.level))
    if level not in OBSERVABILITY_LEVELS:
        raise _err(f"{where}.level {level!r} not in "
                   f"{list(OBSERVABILITY_LEVELS)}")
    return ObservabilityConfig(level=level)


def _parse_rules(raw: Any, base: RulesConfig | None = None,
                 where: str = "os.rules") -> RulesConfig:
    """`os.rules`, or a project's override of it — field-level, like `_parse_inspect`"""
    base = base or RulesConfig()
    if not isinstance(raw, dict):
        raise _err(f'"{where}" must be an object')
    try:
        disarm = int(raw.get("false_positive_disarm", base.false_positive_disarm))
    except (TypeError, ValueError):
        raise _err(f'"{where}.false_positive_disarm" must be an integer') from None
    if disarm < 1:
        raise _err(f'"{where}.false_positive_disarm" must be >= 1')
    return RulesConfig(enabled=bool(raw.get("enabled", base.enabled)),
                       false_positive_disarm=disarm)


def _parse_evolution(raw: Any, base: EvolutionConfig | None = None,
                     where: str = "os.evolution") -> EvolutionConfig:
    """`os.evolution`, or a project's override of it — field-level, like `_parse_inspect`"""
    base = base or EvolutionConfig()
    if not isinstance(raw, dict):
        raise _err(f'"{where}" must be an object')
    min_samples = int(raw.get("min_samples", base.min_samples))
    if min_samples < 1:
        raise _err(f"{where}.min_samples must be 1 or more, got {min_samples}")
    money = {}
    for key in ("weekly_usd", "critical_weekly_usd", "yield_above_usd", "order_budget_usd"):
        value = float(raw.get(key, getattr(base, key)))
        if value < 0:
            raise _err(f"{where}.{key} must be 0 or more, got {value}")
        money[key] = value
    # `critical_weekly_usd` above `weekly_usd` is not refused: the steward checks both
    counts = {}
    for key in ("max_depth", "major_min_refs", "medium_min_refs"):
        value = int(raw.get(key, getattr(base, key)))
        if value < 0:
            raise _err(f"{where}.{key} must be 0 or more, got {value}")
        counts[key] = value
    by_kind = raw.get("min_refs_by_kind", {})
    if not isinstance(by_kind, dict):
        raise _err(f"{where}.min_refs_by_kind must be an object of origin kind -> count")
    from .steward import ORIGIN_KINDS

    # Merged over the base: setting one kind does not silently drop the shipped `neo_rider`
    counts["min_refs_by_kind"] = dict(base.min_refs_by_kind)
    for kind, raw_value in by_kind.items():
        if kind not in ORIGIN_KINDS:
            raise _err(f"{where}.min_refs_by_kind.{kind}: not an origin kind; "
                       f"one of {list(ORIGIN_KINDS)}")
        value = int(raw_value)
        if value < 0:
            raise _err(f"{where}.min_refs_by_kind.{kind} must be 0 or more, got {value}")
        counts["min_refs_by_kind"][kind] = value
    min_precision = float(raw.get("min_precision", base.min_precision))
    if not 0 <= min_precision <= 1:
        raise _err(f"{where}.min_precision must be between 0 and 1, got {min_precision}")
    mode = str(raw.get("mode", base.mode))
    if mode not in EVOLUTION_MODES:
        raise _err(f"{where}.mode must be one of {list(EVOLUTION_MODES)}, got {mode!r}")
    return EvolutionConfig(min_samples=min_samples, min_precision=min_precision,
                           mode=mode, **money, **counts)


def _parse_concision(raw: Any, base: ConcisionConfig | None = None,
                     where: str = "os.concision") -> ConcisionConfig:
    """`os.concision`, or a project's override of it — field-level, like `_parse_inspect`"""
    base = base or ConcisionConfig()
    if not isinstance(raw, dict):
        raise _err(f'"{where}" must be an object')
    words = int(raw.get("summary_max_words", base.summary_max_words))
    if words < 0:
        raise _err(f"{where}.summary_max_words must be 0 (off) or more, got {words}")
    if 0 < words < 20:
        raise _err(f"{where}.summary_max_words of {words} leaves no summary that can "
                   f"pass; use 0 to switch the cap off")
    chars = int(raw.get("message_max_chars", base.message_max_chars))
    if chars < 0:
        raise _err(f"{where}.message_max_chars must be 0 (off) or more, got {chars}")
    if 0 < chars < 1000:
        raise _err(f"{where}.message_max_chars of {chars} leaves no message that can "
                   f"pass; use 0 to switch the cap off")
    # No floor clause for these two: 1000 IS the assume cap (spec §1)
    assume_chars = int(raw.get("assume_max_chars", base.assume_max_chars))
    if assume_chars < 0:
        raise _err(f"{where}.assume_max_chars must be 0 (off) or more, got "
                   f"{assume_chars}")
    question_chars = int(raw.get("question_max_chars", base.question_max_chars))
    if question_chars < 0:
        raise _err(f"{where}.question_max_chars must be 0 (off) or more, got "
                   f"{question_chars}")
    return ConcisionConfig(summary_max_words=words, message_max_chars=chars,
                           assume_max_chars=assume_chars,
                           question_max_chars=question_chars)


def _parse_bugs(raw: Any, base: BugsConfig | None = None,
                where: str = "os.bugs") -> BugsConfig:
    """`os.bugs`, or a project's override of it — field-level, like `_parse_inspect` (#240)"""
    base = base or BugsConfig()
    if not isinstance(raw, dict):
        raise _err(f'"{where}" must be an object')
    label = str(raw.get("label", base.label) or "").strip()
    if not label:
        raise _err(f"{where}.label must not be empty")
    # SHAPE, not just non-emptiness (review round 1)
    from .issues import LABEL_RE
    if not LABEL_RE.match(label):
        raise _err(f"{where}.label must start with a letter or digit and use only "
                   f"letters, digits, spaces and ._:/- (got {label!r})")
    return BugsConfig(label=label)


def _parse_release(raw: Any, base: ReleaseConfig | None = None,
                   where: str = "os.release") -> ReleaseConfig:
    """`os.release`, or a project's override of it — field-level, like `_parse_inspect`"""
    base = base or ReleaseConfig()
    if not isinstance(raw, dict):
        raise _err(f'"{where}" must be an object')
    cfg = ReleaseConfig(
        refile_window_days=int(raw.get("refile_window_days", base.refile_window_days)),
    )
    for name, value in vars(cfg).items():
        if value < 1:
            raise _err(f"{where}.{name} must be >= 1")
    return cfg


def _parse_messaging(raw: Any, base: MessagingConfig | None = None,
                     where: str = "os.messaging") -> MessagingConfig:
    """`os.messaging`, or a project's override of it — field-level, like `_parse_inspect`"""
    base = base or MessagingConfig()
    if not isinstance(raw, dict):
        raise _err(f'"{where}" must be an object')
    cfg = MessagingConfig(
        stuck_minutes=int(raw.get("stuck_minutes", base.stuck_minutes)),
    )
    for name, value in vars(cfg).items():
        if value < 1:
            raise _err(f"{where}.{name} must be >= 1")
    return cfg


def _parse_pull_requests(raw: Any, base: PullRequestsConfig | None = None,
                         where: str = "os.pull_requests") -> PullRequestsConfig:
    """`os.pull_requests`, or a project's override — field-level, like `_parse_messaging`"""
    base = base or PullRequestsConfig()
    if not isinstance(raw, dict):
        raise _err(f'"{where}" must be an object')
    values: dict[str, int] = {}
    for name, default in vars(base).items():
        value = raw.get(name, default)
        if isinstance(value, bool) or not isinstance(value, int):
            raise _err(f"{where}.{name} must be a whole number, got {value!r}")
        if value < 1:
            raise _err(f"{where}.{name} must be >= 1")
        values[name] = value
    cfg = PullRequestsConfig(**values)
    if cfg.catch_up_backoff_base_seconds > cfg.catch_up_backoff_max_seconds:
        raise _err(f"{where}.catch_up_backoff_base_seconds "
                   f"{cfg.catch_up_backoff_base_seconds} is above "
                   f"{where}.catch_up_backoff_max_seconds "
                   f"{cfg.catch_up_backoff_max_seconds}")
    return cfg


def _parse_wiring(raw: Any, base: WiringConfig | None = None,
                  where: str = "os.wiring") -> WiringConfig:
    """`os.wiring`, or a project's override of it — field-level, like `_parse_inspect`"""
    base = base or WiringConfig()
    if not isinstance(raw, dict):
        raise _err(f'"{where}" must be an object')

    def _names(key: str, default: tuple[str, ...]) -> tuple[str, ...]:
        value = raw.get(key, default)
        if isinstance(value, str) or not isinstance(value, (list, tuple)):
            raise _err(f"{where}.{key} must be a list of names")
        return tuple(str(v) for v in value)

    return WiringConfig(
        claude_ai_connectors=bool(raw.get("claude_ai_connectors",
                                          base.claude_ai_connectors)),
        bundled_skills=bool(raw.get("bundled_skills", base.bundled_skills)),
        disabled_plugins=_names("disabled_plugins", base.disabled_plugins),
        disabled_skills=_names("disabled_skills", base.disabled_skills),
        chrome=bool(raw.get("chrome", base.chrome)),
    )


def _parse_worktree(raw: Any, base: WorktreeConfig | None = None,
                    where: str = "os.worktree") -> WorktreeConfig:
    """`os.worktree`, or a project's override of it — field-level, like `_parse_wiring`"""
    base = base or WorktreeConfig()
    if not isinstance(raw, dict):
        raise _err(f'"{where}" must be an object')

    value = raw.get("tool_managed_paths", base.tool_managed_paths)
    if isinstance(value, str) or not isinstance(value, (list, tuple)):
        raise _err(f"{where}.tool_managed_paths must be a list of paths")
    paths = tuple(str(v) for v in value)
    for path in paths:
        if PurePosixPath(path).is_absolute() or Path(path).is_absolute():
            raise _err(f"{where}.tool_managed_paths entry {path!r} must be repo-relative")
        if ".." in PurePosixPath(path).parts:
            raise _err(f"{where}.tool_managed_paths entry {path!r} must not contain '..'")
    hours = raw.get("reclaim_after_hours", base.reclaim_after_hours)
    if isinstance(hours, bool) or not isinstance(hours, int) or hours < 0:
        raise _err(f"{where}.reclaim_after_hours must be a non-negative integer "
                   f"(0 to never reclaim)")
    return WorktreeConfig(tool_managed_paths=paths, reclaim_after_hours=hours)


def _parse_schedule(raw: Any, base: ScheduleConfig | None = None,
                   where: str = "os.schedule") -> ScheduleConfig:
    """`os.schedule`, or a project's override of it — field-level, like `_parse_inspect`"""
    base = base or ScheduleConfig()
    if not isinstance(raw, dict):
        raise _err(f'"{where}" must be an object')
    jobs_raw = raw.get("jobs", base.jobs)
    if isinstance(jobs_raw, str) or not isinstance(jobs_raw, (list, tuple)):
        raise _err(f"{where}.jobs must be a list of job ids "
                   f"(known: {list(schedule_mod.JOB_IDS)})")
    jobs = tuple(str(j) for j in jobs_raw)
    unknown = [j for j in jobs if j not in schedule_mod.JOB_IDS]
    if unknown:
        raise _err(f"{where}.jobs names unknown job(s) {unknown} "
                   f"(known: {list(schedule_mod.JOB_IDS)})")
    cfg = ScheduleConfig(
        enabled=bool(raw.get("enabled", base.enabled)),
        interval_hours=int(raw.get("interval_hours", base.interval_hours)),
        jobs=jobs,
        held_alarm_intervals=int(raw.get("held_alarm_intervals",
                                         base.held_alarm_intervals)),
    )
    for name in ("interval_hours", "held_alarm_intervals"):
        if getattr(cfg, name) < 1:
            raise _err(f"{where}.{name} must be >= 1")
    return cfg


#: Fields of `SupervisorConfig` that are NOT whole numbers
_SUPERVISOR_NON_NUMERIC = ("enabled", "model", "probes", "health_enabled", "remedies",
                           "health_reassert_blockers")


def _parse_remedies(raw: Any, base: RemedyConfig, where: str) -> RemedyConfig:
    """`supervisor.remedies`, or a project's override of it — field-level, like the rest"""
    from . import remedies as remedies_mod

    if raw is None:
        return base
    if not isinstance(raw, dict):
        raise _err(f'"{where}" must be an object')
    allowed_raw = raw.get("allowed", base.allowed)
    if isinstance(allowed_raw, (str, bytes)) or not isinstance(allowed_raw, (list, tuple)):
        raise _err(f'"{where}.allowed" must be a list of remedy ids')
    allowed = tuple(str(item) for item in allowed_raw)
    for remedy_id in allowed:
        if remedy_id not in remedies_mod.REMEDIES:
            raise _err(f"{where}.allowed names unknown remedy {remedy_id!r} — "
                       f"known: {', '.join(remedies_mod.SHIPPED_REMEDIES)}")
    return RemedyConfig(enabled=bool(raw.get("enabled", base.enabled)), allowed=allowed)


def _parse_blockers(raw: Any, base: tuple[str, ...], where: str) -> tuple[str, ...]:
    """`supervisor.health_reassert_blockers`, or a project's override — field-level"""
    if raw is None:
        return tuple(base)
    if isinstance(raw, (str, bytes)) or not isinstance(raw, (list, tuple)):
        raise _err(f'"{where}" must be a list of blocker ids')
    ids = tuple(str(item) for item in raw)
    for blocker_id in ids:
        if blocker_id not in health_mod.BLOCKERS:
            raise _err(f"{where} names unknown blocker {blocker_id!r} — "
                       f"known: {', '.join(health_mod.BLOCKERS)}")
    return ids


def _parse_probes(raw: Any, base: tuple[probes_mod.HealthProbe, ...],
                  where: str) -> tuple[probes_mod.HealthProbe, ...]:
    """`supervisor.probes` merged over `base` by id — `probes.resolve`'s rule (§2)"""
    if raw is None:
        return tuple(base)
    if not isinstance(raw, list):
        raise _err(f'"{where}" must be a list of probe objects')

    known = {p.id for p in base}
    seen: set[str] = set()
    entries: list[dict[str, Any]] = []
    for i, item in enumerate(raw):
        if not isinstance(item, dict):
            raise _err(f"{where}[{i}] must be an object")
        pid = str(item.get("id") or "")
        if not probes_mod.ID_PATTERN.match(pid):
            raise _err(f"{where}[{i}].id {pid!r} must be lowercase kebab-case "
                       f"([a-z0-9-])")
        if pid in seen:
            raise _err(f"{where}: duplicate probe id {pid!r}")
        seen.add(pid)
        entry: dict[str, Any] = {"id": pid}
        if "title" in item:
            entry["title"] = str(item["title"])
        if "prompt" in item:
            entry["prompt"] = str(item["prompt"])
        if "enabled" in item:
            entry["enabled"] = bool(item["enabled"])
        if "subjects" in item:
            subjects = item["subjects"]
            if not isinstance(subjects, list) or not subjects:
                raise _err(f"{where}: probe {pid!r} subjects must be a non-empty list")
            for subject in subjects:
                if subject not in probes_mod.SUBJECTS:
                    raise _err(f"{where}: probe {pid!r} names unknown subject "
                               f"{str(subject)!r}, not one of "
                               f"{list(probes_mod.SUBJECTS)}")
            entry["subjects"] = tuple(str(s) for s in subjects)
        if pid not in known:
            missing = [k for k in ("title", "prompt") if k not in entry]
            if missing:
                raise _err(f"{where}: new probe {pid!r} must give "
                           f"{' and '.join(missing)}")
        entries.append(entry)
    return probes_mod.resolve(base, entries)


def _check_probes(cfg: SupervisorConfig, where: str) -> None:
    """The refusals that need the RESOLVED list and the numbers beside it, so they run after both exist"""
    for probe in cfg.probes:
        if probe.id in probes_mod.RESERVED_IDS:
            raise _err(f"{where}: probe id {probe.id!r} is one of `jarvis inspect`'s "
                       f"alarm kinds — two different things would read as one on every "
                       f"surface")
        if len(probe.prompt) > cfg.probe_prompt_chars:
            raise _err(f"{where}: probe {probe.id!r} prompt is {len(probe.prompt)} "
                       f"characters, over {where}.probe_prompt_chars "
                       f"({cfg.probe_prompt_chars})")
    enabled = [p.id for p in cfg.probes if p.enabled]
    if len(enabled) > cfg.max_enabled_probes:
        raise _err(f"{where}: {len(enabled)} probes enabled, over "
                   f"{where}.max_enabled_probes ({cfg.max_enabled_probes}) — they all "
                   f"ride in one prompt: {', '.join(sorted(enabled))}")


def _parse_supervisor(raw: Any, base: SupervisorConfig | None = None,
                      where: str = "os.supervisor") -> SupervisorConfig:
    """`os.supervisor`, or a project's override of it — §2, and `_parse_inspect`'s shape"""
    base = base or SupervisorConfig()
    if not isinstance(raw, dict):
        raise _err(f'"{where}" must be an object')
    numbers = {k: v for k, v in vars(base).items()
               if k not in _SUPERVISOR_NON_NUMERIC}
    cfg = SupervisorConfig(
        enabled=bool(raw.get("enabled", base.enabled)),
        model=str(raw.get("model", base.model) or base.model),
        health_enabled=bool(raw.get("health_enabled", base.health_enabled)),
        probes=_parse_probes(raw.get("probes"), base.probes, f"{where}.probes"),
        health_reassert_blockers=_parse_blockers(
            raw.get("health_reassert_blockers"), base.health_reassert_blockers,
            f"{where}.health_reassert_blockers"),
        remedies=_parse_remedies(raw.get("remedies"), base.remedies,
                                 f"{where}.remedies"),
        **{k: int(raw.get(k, v)) for k, v in numbers.items()},
    )
    for name, value in vars(cfg).items():
        if isinstance(value, int) and not isinstance(value, bool) and value < 1:
            raise _err(f"{where}.{name} must be >= 1")
    if cfg.timeout >= cfg.stale_reviewing_seconds:
        raise _err(f"{where}.timeout must be under {where}.stale_reviewing_seconds "
                   f"({cfg.stale_reviewing_seconds}s), or an alarm is judged twice")
    _check_probes(cfg, where)
    return cfg


def parse_catalog(data: Any, source_path: Path | None = None) -> Catalog:
    if not isinstance(data, dict):
        raise _err("top level must be an object")

    os_raw = data.get("os", {})
    defaults = os_raw.get("defaults", {})
    notif = os_raw.get("notifications", {})
    telegram = notif.get("telegram", {})
    ui = os_raw.get("ui", {})

    neo_raw = os_raw.get("neo", {})
    if not isinstance(neo_raw, dict):
        raise _err('"os.neo" must be an object')
    neo_cfg = NeoConfig(
        enabled=bool(neo_raw.get("enabled", True)),
        model=neo_raw.get("model", "opus"),
        learnings_limit=int(neo_raw.get("learnings_limit", 50)),
        timeout=int(neo_raw.get("timeout", 300)),
        digest_model=str(neo_raw.get("digest_model", "haiku")),
        digest_max_question_chars=_digest_max_question_chars_or_err(neo_raw),
        review_findings=bool(neo_raw.get("review_findings", True)),
        panel=_parse_panel(neo_raw.get("panel", {})),
    )

    cold_floor, cold_floor_max = _cold_prefix_floor_or_err(os_raw)
    health_days, health_orders, health_bounds, health_prefix = _cache_health_or_err(os_raw)
    os_cfg = OsConfig(
        default_model=defaults.get("model", DEFAULT_MODEL),
        default_effort=defaults.get("effort"),
        default_permission_mode=defaults.get("permission_mode", DEFAULT_PERMISSION_MODE),
        default_bash_first=defaults.get("bash_first", DEFAULT_WORKER_BASH_FIRST),
        default_tool_search=defaults.get("tool_search", DEFAULT_WORKER_TOOL_SEARCH),
        # Spec 2026-10-15-the-hour-on-worker-turns-only.md §1
        default_cache_ttl=defaults.get("cache_ttl", DEFAULT_WORKER_CACHE_TTL),
        default_transport=defaults.get("transport", DEFAULT_WORKER_TRANSPORT),
        # Spec 2026-10-02-serena-the-cheap-path.md §6
        default_py_nav_hook=defaults.get("py_nav_hook", DEFAULT_WORKER_PY_NAV_HOOK),
        # Spec 2026-10-06-navigate-specs-like-code.md §5
        default_doc_nav_hook=defaults.get("doc_nav_hook", DEFAULT_WORKER_DOC_NAV_HOOK),
        default_doc_read_limit_lines=_doc_read_limit_or_err(
            defaults, "os.defaults.doc_read_limit_lines",
            DEFAULT_WORKER_DOC_READ_LIMIT_LINES),
        default_max_concurrent=int(defaults.get("max_concurrent", DEFAULT_MAX_CONCURRENT)),
        max_in_flight=int(defaults.get("max_in_flight", DEFAULT_MAX_IN_FLIGHT)),
        default_autocompact_window=_autocompact_or_err(
            defaults, "autocompact_window", "os.defaults.autocompact_window",
            DEFAULT_AUTOCOMPACT_WINDOW),
        default_budget_usd=_parse_budget(
            defaults, "budget_usd", "os.defaults.budget_usd", DEFAULT_BUDGET_USD),
        default_feature_budget_usd=_parse_budget(
            defaults, "feature_budget_usd", "os.defaults.feature_budget_usd",
            DEFAULT_FEATURE_BUDGET_USD),
        notification_sinks=notif.get("sinks", ["log"]),
        telegram_token_env=telegram.get("token_env", "JARVIS_TELEGRAM_TOKEN"),
        telegram_chat_id_env=telegram.get("chat_id_env", "JARVIS_TELEGRAM_CHAT_ID"),
        ui_port=ui.get("port", 8787),
        ui_base_url=str(ui.get("base_url", "") or "").rstrip("/"),
        cold_prefix_floor=cold_floor,
        cold_prefix_floor_max=cold_floor_max,
        cache_health_window_days=health_days,
        cache_health_min_orders=health_orders,
        cache_health_min_boundaries=health_bounds,
        cache_health_prefix_share=health_prefix,
        compact_min_context=_compact_min_context_or_err(os_raw),
        max_os_prompt_chars=_max_os_prompt_chars_or_err(os_raw),
        disk_free_warn_gb=_disk_free_warn_gb_or_err(os_raw),
        knowledge_inject_limit=int(os_raw.get("knowledge_inject_limit", 8)),
        knowledge_digest_limit=int(os_raw.get("knowledge_digest_limit", 8)),
        knowledge_digest_chars=int(os_raw.get("knowledge_digest_chars", 1000)),
        knowledge_pinned_chars=int(os_raw.get("knowledge_pinned_chars", 360)),
        knowledge_hint_limit=int(os_raw.get("knowledge_hint_limit", 3)),
        knowledge_hint_chars=int(os_raw.get("knowledge_hint_chars", 400)),
        neo=neo_cfg,
        validation=_parse_validation(os_raw.get("validation", {})),
        inspect=_parse_inspect(os_raw.get("inspect", {})),
        ui_health=_parse_ui_health(os_raw.get("ui_health", {})),
        fleet_health=_parse_fleet_health(os_raw.get("fleet_health", {})),
        navigation=_parse_navigation(os_raw.get("navigation", {})),
        cost=_parse_cost(os_raw.get("cost", {})),
        observability=_parse_observability(os_raw.get("observability", {})),
        concision=_parse_concision(os_raw.get("concision", {})),
        supervisor=_parse_supervisor(os_raw.get("supervisor", {})),
        rules=_parse_rules(os_raw.get("rules", {})),
        evolution=_parse_evolution(os_raw.get("evolution", {})),
        messaging=_parse_messaging(os_raw.get("messaging", {})),
        pull_requests=_parse_pull_requests(os_raw.get("pull_requests", {})),
        bugs=_parse_bugs(os_raw.get("bugs", {})),
        release=_parse_release(os_raw.get("release", {})),
        schedule=_parse_schedule(os_raw.get("schedule", {})),
        wiring=_parse_wiring(os_raw.get("wiring", {})),
        worktree=_parse_worktree(os_raw.get("worktree", {})),
        autonomy=_parse_autonomy(os_raw.get("autonomy", {})),
        morpheus=_parse_morpheus(os_raw.get("morpheus", {})),
    )
    if os_cfg.default_permission_mode not in VALID_PERMISSION_MODES:
        raise _err(f"os.defaults.permission_mode {os_cfg.default_permission_mode!r} not in {sorted(VALID_PERMISSION_MODES)}")
    # Spec 2026-10-01-the-steer-that-beat-the-brief.md §1
    if os_cfg.default_bash_first not in VALID_BASH_FIRST:
        raise _err(f"os.defaults.bash_first {os_cfg.default_bash_first!r} not in "
                   f"{sorted(VALID_BASH_FIRST)}")
    # Spec 2026-10-02-serena-the-cheap-path.md §4
    if os_cfg.default_tool_search not in VALID_TOOL_SEARCH:
        raise _err(f"os.defaults.tool_search {os_cfg.default_tool_search!r} not in "
                   f"{sorted(VALID_TOOL_SEARCH)}")
    # Spec 2026-10-15-the-hour-on-worker-turns-only.md §1
    if os_cfg.default_cache_ttl not in VALID_WORKER_CACHE_TTL:
        raise _err(f"os.defaults.cache_ttl {os_cfg.default_cache_ttl!r} not in "
                   f"{sorted(VALID_WORKER_CACHE_TTL)}")
    if os_cfg.default_transport not in VALID_WORKER_TRANSPORT:
        raise _err(f"os.defaults.transport {os_cfg.default_transport!r} not in "
                   f"{sorted(VALID_WORKER_TRANSPORT)}")
    # Spec 2026-10-02-serena-the-cheap-path.md §6
    if os_cfg.default_py_nav_hook not in VALID_PY_NAV_HOOK:
        raise _err(f"os.defaults.py_nav_hook {os_cfg.default_py_nav_hook!r} not in "
                   f"{sorted(VALID_PY_NAV_HOOK)}")
    # Spec 2026-10-06-navigate-specs-like-code.md §5
    if os_cfg.default_doc_nav_hook not in VALID_DOC_NAV_HOOK:
        raise _err(f"os.defaults.doc_nav_hook {os_cfg.default_doc_nav_hook!r} not in "
                   f"{sorted(VALID_DOC_NAV_HOOK)}")
    if os_cfg.default_doc_read_limit_lines < WORKER_DOC_READ_LIMIT_LINES_MIN:
        raise _err(f"os.defaults.doc_read_limit_lines must be >= "
                   f"{WORKER_DOC_READ_LIMIT_LINES_MIN}, got "
                   f"{os_cfg.default_doc_read_limit_lines}")
    if os_cfg.default_max_concurrent < 1:
        raise _err("os.defaults.max_concurrent must be >= 1")
    if os_cfg.max_in_flight < 1:
        raise _err("os.defaults.max_in_flight must be >= 1")

    projects_raw = data.get("projects", [])
    if not isinstance(projects_raw, list):
        raise _err('"projects" must be a list')
    # An empty fleet is valid: a standby instance (e.g. a fresh production deployment) boots with no...

    projects: list[ProjectSpec] = []
    seen: set[str] = set()
    for i, p in enumerate(projects_raw):
        if not isinstance(p, dict):
            raise _err(f"projects[{i}] must be an object")
        name = p.get("name")
        if not name or not isinstance(name, str):
            raise _err(f"projects[{i}].name is required")
        if name in seen:
            raise _err(f"duplicate project name {name!r}")
        seen.add(name)
        raw_path = p.get("path")
        if not raw_path:
            raise _err(f"projects[{i}] ({name}): path is required")
        ppath = Path(raw_path).expanduser().resolve()

        w = p.get("worker", {})
        pmode = w.get("permission_mode", os_cfg.default_permission_mode)
        if pmode not in VALID_PERMISSION_MODES:
            raise _err(f"project {name}: worker.permission_mode {pmode!r} invalid")
        # Spec 2026-10-01-the-steer-that-beat-the-brief.md §1
        bash_first = w.get("bash_first", os_cfg.default_bash_first)
        if bash_first not in VALID_BASH_FIRST:
            raise _err(f"project {name}: worker.bash_first {bash_first!r} not in "
                       f"{sorted(VALID_BASH_FIRST)}")
        # Spec 2026-10-02-serena-the-cheap-path.md §4
        tool_search = w.get("tool_search", os_cfg.default_tool_search)
        if tool_search not in VALID_TOOL_SEARCH:
            raise _err(f"project {name}: worker.tool_search {tool_search!r} not in "
                       f"{sorted(VALID_TOOL_SEARCH)}")
        # Spec 2026-10-15-the-hour-on-worker-turns-only.md §1
        cache_ttl = w.get("cache_ttl", os_cfg.default_cache_ttl)
        if cache_ttl not in VALID_WORKER_CACHE_TTL:
            raise _err(f"project {name}: worker.cache_ttl {cache_ttl!r} not in "
                       f"{sorted(VALID_WORKER_CACHE_TTL)}")
        transport = w.get("transport", os_cfg.default_transport)
        if transport not in VALID_WORKER_TRANSPORT:
            raise _err(f"project {name}: worker.transport {transport!r} not in "
                       f"{sorted(VALID_WORKER_TRANSPORT)}")
        # Spec 2026-10-02-serena-the-cheap-path.md §6
        py_nav_hook = w.get("py_nav_hook", os_cfg.default_py_nav_hook)
        if py_nav_hook not in VALID_PY_NAV_HOOK:
            raise _err(f"project {name}: worker.py_nav_hook {py_nav_hook!r} not in "
                       f"{sorted(VALID_PY_NAV_HOOK)}")
        # Spec 2026-10-06-navigate-specs-like-code.md §5
        doc_nav_hook = w.get("doc_nav_hook", os_cfg.default_doc_nav_hook)
        if doc_nav_hook not in VALID_DOC_NAV_HOOK:
            raise _err(f"project {name}: worker.doc_nav_hook {doc_nav_hook!r} not in "
                       f"{sorted(VALID_DOC_NAV_HOOK)}")
        doc_read_limit_lines = _doc_read_limit_or_err(
            w, f"project {name}: worker.doc_read_limit_lines",
            os_cfg.default_doc_read_limit_lines)
        max_conc = int(p.get("max_concurrent", os_cfg.default_max_concurrent))
        if max_conc < 1:
            raise _err(f"project {name}: max_concurrent must be >= 1")
        try:
            launcher = parse_launcher(w.get("launcher"), f"project {name}: worker.launcher")
        except ValueError as e:
            raise _err(str(e)) from e
        if launcher is not None and launcher.open and transport != "background":
            raise _err(f"project {name}: worker.launcher.open opens a background session, "
                       f"so worker.transport must be background (got {transport!r})")
        require_crew = w.get("require_crew", False)
        if not isinstance(require_crew, bool):
            raise _err(f"project {name}: worker.require_crew must be true or false, "
                       f"got {require_crew!r}")
        mcp_timeout = int(w.get("mcp_tool_timeout_ms",
                                DEFAULT_WORKER_MCP_TOOL_TIMEOUT_MS))
        if mcp_timeout < WORKER_MCP_TOOL_TIMEOUT_MS_MIN:
            raise _err(f"project {name}: worker.mcp_tool_timeout_ms must be >= "
                       f"{WORKER_MCP_TOOL_TIMEOUT_MS_MIN} (milliseconds — a ceiling "
                       f"under a second would fail every MCP call), got {mcp_timeout}")
        denied_tools = w.get("denied_tools", list(DEFAULT_WORKER_DENIED_TOOLS))
        if (not isinstance(denied_tools, list)
                or not all(isinstance(t, str) and t for t in denied_tools)):
            raise _err(f"project {name}: worker.denied_tools must be a list of tool "
                       f"names, got {denied_tools!r}")
        worker = WorkerDefaults(
            model=w.get("model") or p.get("model") or os_cfg.default_model,
            effort=w.get("effort", os_cfg.default_effort),
            permission_mode=pmode,
            bash_first=bash_first,
            tool_search=tool_search,
            cache_ttl=cache_ttl,
            transport=transport,
            launcher=launcher,
            py_nav_hook=py_nav_hook,
            doc_nav_hook=doc_nav_hook,
            doc_read_limit_lines=doc_read_limit_lines,
            append_system_prompt=w.get("append_system_prompt"),
            autocompact_window=_autocompact_or_err(
                w, "autocompact_window",
                f"project {name}: worker.autocompact_window",
                os_cfg.default_autocompact_window),
            budget_usd=_parse_budget(
                w, "budget_usd", f"project {name}: worker.budget_usd",
                os_cfg.default_budget_usd),
            feature_budget_usd=_parse_budget(
                w, "feature_budget_usd", f"project {name}: worker.feature_budget_usd",
                os_cfg.default_feature_budget_usd),
            investigation_budget_usd=_parse_budget(
                w, "investigation_budget_usd",
                f"project {name}: worker.investigation_budget_usd",
                DEFAULT_INVESTIGATION_BUDGET_USD),
            investigation_model=w.get("investigation_model",
                                      DEFAULT_INVESTIGATION_MODEL),
            investigation_effort=w.get("investigation_effort",
                                       DEFAULT_INVESTIGATION_EFFORT),
            require_crew=require_crew,
            mcp_tool_timeout_ms=mcp_timeout,
            denied_tools=tuple(denied_tools),
        )
        try:
            gate_cfg = GateConfig.parse(p.get("gates"))
        except ValueError as e:
            raise _err(f"project {name}: {e}") from e
        validation_cfg = _parse_validation(
            p.get("validation", {}), base=os_cfg.validation,
            where=f"projects[{i}] ({name}).validation")
        inspect_cfg = _parse_inspect(
            p.get("inspect", {}), base=os_cfg.inspect,
            where=f"projects[{i}] ({name}).inspect")
        ui_health_cfg = _parse_ui_health(
            p.get("ui_health", {}), base=os_cfg.ui_health,
            where=f"projects[{i}] ({name}).ui_health")
        fleet_health_cfg = _parse_fleet_health(
            p.get("fleet_health", {}), base=os_cfg.fleet_health,
            where=f"projects[{i}] ({name}).fleet_health")
        navigation_cfg = _parse_navigation(
            p.get("navigation", {}), base=os_cfg.navigation,
            where=f"projects[{i}] ({name}).navigation")
        cost_cfg = _parse_cost(
            p.get("cost", {}), base=os_cfg.cost,
            where=f"projects[{i}] ({name}).cost")
        observability_cfg = _parse_observability(
            p.get("observability", {}), base=os_cfg.observability,
            where=f"projects[{i}] ({name}).observability")
        concision_cfg = _parse_concision(
            p.get("concision", {}), base=os_cfg.concision,
            where=f"projects[{i}] ({name}).concision")
        supervisor_cfg = _parse_supervisor(
            p.get("supervisor", {}), base=os_cfg.supervisor,
            where=f"projects[{i}] ({name}).supervisor")
        rules_cfg = _parse_rules(
            p.get("rules", {}), base=os_cfg.rules,
            where=f"projects[{i}] ({name}).rules")
        evolution_cfg = _parse_evolution(
            p.get("evolution", {}), base=os_cfg.evolution,
            where=f"projects[{i}] ({name}).evolution")
        messaging_cfg = _parse_messaging(
            p.get("messaging", {}), base=os_cfg.messaging,
            where=f"projects[{i}] ({name}).messaging")
        pull_requests_cfg = _parse_pull_requests(
            p.get("pull_requests", {}), base=os_cfg.pull_requests,
            where=f"projects[{i}] ({name}).pull_requests")
        bugs_cfg = _parse_bugs(
            p.get("bugs", {}), base=os_cfg.bugs,
            where=f"projects[{i}] ({name}).bugs")
        release_cfg = _parse_release(
            p.get("release", {}), base=os_cfg.release,
            where=f"projects[{i}] ({name}).release")
        schedule_cfg = _parse_schedule(
            p.get("schedule", {}), base=os_cfg.schedule,
            where=f"projects[{i}] ({name}).schedule")
        wiring_cfg = _parse_wiring(
            p.get("wiring", {}), base=os_cfg.wiring,
            where=f"projects[{i}] ({name}).wiring")
        worktree_cfg = _parse_worktree(
            p.get("worktree", {}), base=os_cfg.worktree,
            where=f"projects[{i}] ({name}).worktree")
        projects.append(
            ProjectSpec(
                name=name,
                path=ppath,
                description=p.get("description", ""),
                model=p.get("model") or os_cfg.default_model,
                worker=worker,
                settings_overrides=p.get("settings_overrides", {}),
                max_concurrent=max_conc,
                gates=gate_cfg,
                validation=validation_cfg,
                inspect=inspect_cfg,
                ui_health=ui_health_cfg,
                fleet_health=fleet_health_cfg,
                navigation=navigation_cfg,
                cost=cost_cfg,
                observability=observability_cfg,
                concision=concision_cfg,
                supervisor=supervisor_cfg,
                rules=rules_cfg,
                evolution=evolution_cfg,
                messaging=messaging_cfg,
                pull_requests=pull_requests_cfg,
                bugs=bugs_cfg,
                release=release_cfg,
                schedule=schedule_cfg,
                wiring=wiring_cfg,
                worktree=worktree_cfg,
                raw=p,
            )
        )

    return Catalog(os=os_cfg, projects=projects, source_path=source_path)


def validate_paths(catalog: Catalog) -> list[str]:
    """Return human-readable problems with project paths (missing dir, not a git repo)"""
    problems = []
    for p in catalog.projects:
        if not p.path.is_dir():
            problems.append(f"{p.name}: path does not exist: {p.path}")
        elif not (p.path / ".git").exists():
            problems.append(f"{p.name}: not a git repository ({p.path}) — run `git init` first")
    return problems
