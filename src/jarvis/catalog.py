"""Catalog: the JSON file describing the fleet of projects Jarvis manages."""

from __future__ import annotations

import json
import math
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import concision
from . import probes as probes_mod
from . import schedule as schedule_mod
from .gates import GateConfig
from .neo_store import Q_KINDS, SEATS
from .project_store import VALIDATOR_SEATS

# Dotted-path globs (fnmatch) over a resolved config map naming the settings that are
# SAFETY rather than money: they change what a worker is allowed to do. Earns its place
# by buying exactly two things and no more — a louder confirmation, and a mandatory
# `--reason` on the version row. See
# docs/superpowers/specs/2026-08-27-the-config-console.md §7, and §11.5 for what the
# list is not yet sure of.
SAFETY_KEYS = (
    "*.permission_mode",
    "*.gates.*",
    # `*.` rather than `os.`: a project's own validation block is the same switch with a
    # smaller blast radius, and the per-project form is the sentence the design's
    # acceptance walk is written in (§10.3).
    "*.validation.*",
    "os.neo.enabled",
    # Same class of act as turning Neo off: it removes a reviewer, and the change is
    # invisible on every surface until the thing it was reviewing goes wrong.
    "os.supervisor.enabled",
    # And the same again for the health sweep, which is a separate watcher with a
    # separate switch — docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md §2.
    "os.supervisor.health_enabled",
    # What the OS is permitted to DO, not merely to watch (§5). `*.` rather than `os.`
    # for `*.validation.*`'s reason: the per-project form is the same switch with a
    # smaller blast radius, and both halves — the flag and the allow-list — widen it.
    "*.supervisor.remedies.*",
    # The whole scheduler block, not just its switch. It is the only thing in the OS
    # that files work with nobody asking, so enabling it and shortening its interval are
    # the same act at two magnitudes — and a change here is invisible until the morning
    # it starts spending. docs/superpowers/specs/2026-09-14-the-scheduler.md §2.
    "*.schedule.*",
)

# Mirrors `claude --permission-mode` choices exactly (CLI rejects anything else).
VALID_PERMISSION_MODES = {
    "acceptEdits",
    "auto",
    "bypassPermissions",
    "manual",
    "dontAsk",
    "plan",
}

# Default worker mode. `auto` runs routine tools (grep, edits, scripts, tests, git)
# without a prompt per action — the only way a `--bg` worker can run unattended, since
# a background session can't answer a prompt. Sensitive paths stay protected by the
# project's PreToolUse deny guards (catalog settings_overrides), which fire in every
# mode; `auto` does not weaken those. See ASSUMPTIONS.md §9.
DEFAULT_PERMISSION_MODE = "auto"

# Model every worker runs on unless the catalog overrides it (os.defaults.model, a
# project's `model`, or per work order via `jarvis wo create --model`). Passed straight
# through to `claude --model`, so it accepts a full model id (pinned, as here) or an
# alias like `opus`/`sonnet` (which floats to whatever is latest in that tier).
DEFAULT_MODEL = "claude-opus-5"

# Modes in which a `--bg` worker never stalls waiting for a human: `auto` (classifier
# vets each action), `bypassPermissions` (no checks), and `dontAsk` (unlisted tools are
# denied, not prompted). Every OTHER mode — acceptEdits, manual/default, plan — prompts
# on tool calls a real task needs (git, tests, scripts), and a background session can't
# answer, so it hangs. `worker_stalls_on_prompts()` flags those for the user.
AUTONOMOUS_PERMISSION_MODES = {"auto", "bypassPermissions", "dontAsk"}


def worker_stalls_on_prompts(mode: str) -> bool:
    """True when a background worker in this permission mode will block on a prompt."""
    return mode not in AUTONOMOUS_PERMISSION_MODES


# Default simultaneous work orders per project; the rest queue (catalog-tunable per
# project, or fleet-wide via os.defaults.max_concurrent). A slot is spent by a turn that
# is executing, not by a record waiting on somebody — `project_store.SLOT_STATUSES`.
DEFAULT_MAX_CONCURRENT = 5


# -- the account, which is not a project -------------------------------------------
#
# `max_concurrent` above caps a PROJECT. The thing that ran out on 2026-09-02 was the
# ACCOUNT: four children of one feature order launched together, and three of them paid
# 51-72s of Opus time to be told the session window was already spent. A per-project cap
# could not have prevented that and no arrangement of per-project caps can, because the
# limit is not divided among projects. Hence one number, fleet-wide, with no per-project
# override to layer: the resource being rationed is shared, so a project that named its
# own share would be naming a share of something it does not own.
#
# THE UNIT IS A TURN IN FLIGHT (`wo_turns.state='running'`), not a work-order status.
# The two now agree about parked orders — issue #134 took `waiting_input` and
# `validating` out of `count_active` for the same reason this cap never counted them —
# but they still differ: a usage-limit pause leaves its order `running`, spending a
# project slot while it draws nothing from the account. See src/jarvis/fleet.py.
#
# 3, because the incident ran four Opus workers plus a planner while Neo answers and
# validation-panel seats drew on the same account (Neo, question 219). It leaves headroom
# for those without serialising the fleet.
DEFAULT_MAX_IN_FLIGHT = 3


# How large a worker's conversation is allowed to grow before Claude Code compacts it
# (`claude --autocompact <tokens>`). This is the single biggest lever the OS has on its
# own bill and it is on by default — see
# docs/superpowers/specs/2026-08-10-resume-cost-and-the-cache.md.
#
# WHY A NUMBER AND NOT "auto": left alone, a worker on a 1M-token model does not compact
# until ~800k, so every API call it makes re-reads the whole conversation. Cache READ is
# 56% of everything Jarvis spends (kn-1485b845) and it is linear in this number: 146 API
# calls against a 250-290k context cost 26.3M read tokens in fifteen minutes, measured on
# wo-996c7344 and wo-67d4f8b0. Bounding the context bounds every one of those reads.
#
# WHAT THE NUMBER MEANS: it is the effective context WINDOW, not the trigger point. The
# CLI takes min(model window, this) and arms auto-compact at a model-table fraction of
# it, so 400,000 caps a worker's context a little under 400k rather than at it.
#
# WHY 400,000 AND NOT LESS: the cost of setting it too low is a worker that compacts
# mid-task and loses detail. The first value shipped was 150,000, which sits close enough
# to real work to bite — 10% of sessions peak over 120k and 6% over 150k (kn-f94abf34) —
# so the orders it truncated were the long ones, exactly where losing detail hurts most.
# 400,000 is the user's ruling on wo-6808dd2d. It still halves what an unbounded worker on
# a 1M model would reach (~800k before it compacts at all), so the bound and its linear
# saving on every cache read remain; it just leaves compaction an exception rather than a
# routine event. The CLI accepts 100k-1M and rejects anything outside; a project can move
# it either way, or set it to null to opt out and take the model's own window.
DEFAULT_AUTOCOMPACT_WINDOW = 400_000
# A STANDING DOLLAR BUDGET for every new order in a project, in US dollars, or None for
# no ceiling — which is the default and is what the OS did before budgets existed. An OS
# that starts refusing to work because of a number nobody set would be worse than the
# problem, so nothing here is populated by default and a project opts in.
#
# Two settings rather than one scaled from the other. A feature order's budget bounds its
# whole family (planner, manager, every child), and nothing at the moment the default is
# written knows how many children a feature will have — so a per-work-order number says
# nothing useful about a family, and a multiple of it would be a guess wearing a
# configuration's clothes. A project that sets only one gets a ceiling on that kind of
# order alone.
#
# Resolved at CREATION and stamped onto the row, unlike `autocompact_window` just above,
# which is re-read from the catalog on every turn. Deliberately the opposite choice: a
# budget is a contract about ONE order that `jarvis wo show` has to be able to state, and
# lowering a fleet default must not strand work the user already authorised at the old
# number. See src/jarvis/budget.py.
DEFAULT_BUDGET_USD: float | None = None
DEFAULT_FEATURE_BUDGET_USD: float | None = None


def _parse_budget(raw: dict[str, Any], key: str, where: str,
                  default: float | None) -> float | None:
    """Validate a dollar budget. An explicit null means "no ceiling".

    Rejected at load time for the reason the autocompact window is: a bad value would
    otherwise surface as a refused `claude` invocation on the first dispatch, hours after
    the edit that caused it. Zero is refused rather than read as "no ceiling" — it looks
    like an instruction to spend nothing, and silently inverting that is the one
    misreading that costs money.

    `NaN` IS REFUSED HERE AND NOT ONLY IN `budget.parse_amount`, because `json.loads`
    accepts a bare `NaN` literal, so a catalog can carry one and this is the only thing
    between it and every new order in the project. Its danger is spelled out in that
    function: a NaN budget fails OPEN while rendering as enabled, and stamped on a
    project default it does so fleet-wide and silently. `isinstance(value, float)` is
    true of it, and so is `not (value <= 0)`.
    """
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

# A cold cache boundary that kept NOTHING was the entry expiring; one that still served
# the static head of the system prompt was the prefix moving, and no TTL would have
# helped it. This is the ceiling separating them, and it is configurable because it is a
# property of THIS FLEET'S PROMPTS — the static head is a project's CLAUDE.md plus the
# worker briefing, so a fleet of terse projects sits lower and a verbose one higher.
#
# IT SITS AT `os.` AND NOT AT `os.defaults.`, WHICH IS THE DELIBERATE PART. Everything
# under `os.defaults` is a value a project may override, and per-project is the shape
# this setting most looks like it wants — the static head is literally per project. It
# is not offered, because the two surfaces that consume the threshold (the fleet cost
# view and scripts/cache_ttl_cohort.py) walk TRANSCRIPTS rather than work orders and
# cannot cheaply tell which project a session belonged to. A per-project override would
# therefore be honoured for a single order's bill and silently ignored by every
# aggregate — a knob that looks like a feature and behaves like a bug. Offer it only
# together with session-to-project attribution in those two readers (Neo, question 191).
#
# Reasoning and the measured populations:
# docs/superpowers/findings/2026-08-30-where-the-800-dollars-went.md.
DEFAULT_COLD_PREFIX_FLOOR = 5_000
#: Guard rail on the above, and configurable for the same reason it is: a fleet whose
#: static heads are unusually large needs room to raise the floor. Only the SCHEMA
#: defaults are literals here; both values are `os.` keys the config console can change
#: without a release.
DEFAULT_COLD_PREFIX_FLOOR_MAX = 100_000

# -- CACHE HEALTH: the window and the floors `invariants.check_cache_ttl_trigger` and
# `invariants.check_prefix_stable` judge the FLEET's cache configuration over.
#
# At `os.` and not at `os.defaults.` for the reason above, and here the reason is even
# less arguable: both checks decide something there is only one of — the write TTL every
# worker is launched with, and the `includeGitInstructions` line in
# `dispatch._write_worker_settings`. A per-project value would be a per-project answer to
# a fleet-wide question, and the check would report one decision once per project.

#: The cohort window both checks measure over. NEVER all history: the split between the
#: two causes is drifting, and an average over everything averages the trend away
#: (kn-1449447a (5)). Thirty days because that is what `bill.COHORT_COMMAND` tells the
#: reader to run — the doctor and the script differ on population, and they should not
#: also differ on window.
DEFAULT_CACHE_HEALTH_WINDOW_DAYS = 30

#: Below this many MEASURED orders in the window, both checks report nothing at all. THE
#: ANSWER TO "a quiet day gives a wild ratio": a fleet that settled three work orders has
#: a ratio, and it is one order's shape wearing the fleet's name. Measured on this
#: machine 2026-09-16: 51 sealed orders in the trailing 7 days and 226 in 30, so an
#: ordinary week clears this twice over while a genuinely quiet window does not.
DEFAULT_CACHE_HEALTH_MIN_ORDERS = 20

#: …and the floor that actually bounds the noise, because the ratios are token-weighted
#: and a single boundary can carry a 300k write. Below this many observed boundaries the
#: window is a handful of events, not a rate. Measured on the same run: 294 boundaries in
#: 7 days, 752 in 30 — so this is about a sixth of a normal week, and each boundary is at
#: most 2% of the count at the floor itself. Both floors are needed: too few orders is
#: one order's shape, too few boundaries is one boundary's.
DEFAULT_CACHE_HEALTH_MIN_BOUNDARIES = 50

#: Prefix-invalidation writes as a share of ALL cache writes, above which the prefix fix
#: is reported as drifting. Measured over the sealed-bill population on 2026-09-16:
#: 34.6% over 7 days, 35.2% over 30. Set above that deliberately — the same choice
#: `DEFAULT_INSPECT_ALARM_REWRITE_PREFIX_SHARE` makes — so the check reports the prefix
#: GETTING WORSE rather than restating the standing figure the findings doc already
#: recorded. There is no principled break-even for this one, unlike the TTL's: it is an
#: empirical "is this still the number it was".
DEFAULT_CACHE_HEALTH_PREFIX_SHARE = 0.45


# -- COMPACTION AT AN EXPIRED BOUNDARY ------------------------------------------------
#
# The third remedy for the re-write tax, and the only one that acts at the moment the
# cost is about to be paid. `DEFAULT_AUTOCOMPACT_WINDOW` bounds how large a conversation
# may GROW; the one-hour write (`usage.TTL_BREAK_EVEN`) buys a longer entry for every
# token; this compacts a conversation whose entry has ALREADY expired, before the next
# prompt re-sends it. Spec: docs/superpowers/specs/2026-09-18-compact-past-the-ttl.md.
#
# WHY IT PAYS, measured live on 2026-09-18 with a paired probe over a 293,377-token
# conversation (both arms forked from one base, both past the write TTL):
#
#   no compaction   294,007 tokens re-sent as a cache WRITE at 1.25x   $1.853
#   /compact then   282,595 tokens re-sent as PLAIN INPUT at 1.0x      $1.621
#   the same prompt 28,156-token context (15,380 written, 12,776 read) $0.128
#
# Two effects, and the second is much the larger. The re-send itself gets 20% cheaper
# because the CLI does not cache-write a compaction call. And the conversation that
# every later call in that turn re-reads collapses from 293k to 28k — the transcript's
# own `compact_boundary` record puts it at 293,382 tokens in and 4,697 out, the rest of
# the 28k being the static head every call carries anyway.

#: Output tokens one compaction produces. MEASURED on the probe above (8,052 for a
#: 293k conversation; 3,336 for a 26k one), and it is the dominant cost of compacting a
#: SMALL conversation — output is 5x input at Opus list, so a summary that saves
#: nothing still costs ~$0.20. The whole reason there is a floor at all.
COMPACT_SUMMARY_OUTPUT = 8_052

#: What a session's context weighs on the call AFTER a compaction, in tokens. Measured
#: at 28,156 from a 293k conversation and 26,866 from a 26k one — near enough constant
#: across an 11x range of inputs, because what survives is the static head plus a
#: summary whose size is set by the summarisation prompt rather than by what it
#: summarises (4,697 and 4,078 tokens respectively, per the transcript's own
#: `compact_boundary` row). That near-constancy is what makes the break-even below a
#: function of one variable.
COMPACT_CONTEXT = 28_156

#: …of which this much is a cache WRITE on that call; the remainder is the static head,
#: served as a read. Split out because the two are priced 12.5x apart.
COMPACT_FIRST_WRITE = 15_380

#: THE FLOOR: the smallest context worth compacting, in tokens. Below it the re-write
#: is cheaper than the compaction call, so the OS lets the boundary go cold and pays it.
#:
#: MEASURED, not chosen — `scripts/compaction_cohort.py` prices every TTL-expired
#: boundary in the fleet's own transcripts under the model above and reports the net
#: saving by context band. Over the 30 days to 2026-09-19, 414 such boundaries:
#:
#:     25k-50k     33% of boundaries net-positive, median -$0.14
#:     50k-100k    60% net-positive, median +$0.06 to +$0.36, worst -$0.21
#:     100k-150k   80% net-positive, median +$0.39
#:     150k-200k   94%,  200k+  100% net-positive, median +$1.11
#:
#: 100,000 is the lowest band where four boundaries in five pay. It is set where the
#: MAJORITY flips rather than where the MEAN does, because this fires unattended on
#: every work order: everything under it is worth $13.15 of a $552 saving, and half of
#: those boundaries would have lost money. Buying 2.4% of the benefit back would mean
#: being wrong about half the small cases, unsupervised, for ever.
DEFAULT_COMPACT_MIN_CONTEXT: int | None = 100_000

#: Guard rail on the above, for the reason `DEFAULT_COLD_PREFIX_FLOOR_MAX` is one: a
#: fleet may move the floor, and a typo that moved it to 600 would compact every
#: boundary in sight at a loss.
COMPACT_MIN_CONTEXT_MIN = 10_000


_MISSING = object()


def _parse_autocompact(raw: dict[str, Any], key: str, where: str,
                       default: int | None) -> int | None:
    """Validate an autocompact window. An explicit null means "no bound".

    Raises ValueError; callers wrap it with `_err` so the message carries the field.
    Absent and null are deliberately DIFFERENT here: absent inherits the default (which
    is a real bound), null is the opt-out. A project that wants the model's own window
    back has to say so, because silence must not disable a cost control.
    """
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
    """Raised when the catalog file is invalid."""


@dataclass
class WorkerDefaults:
    model: str | None = None
    effort: str | None = None
    permission_mode: str = DEFAULT_PERMISSION_MODE
    append_system_prompt: str | None = None
    # None = no bound (the model's own window stands). See DEFAULT_AUTOCOMPACT_WINDOW.
    autocompact_window: int | None = DEFAULT_AUTOCOMPACT_WINDOW
    # None = no ceiling, which is the default everywhere. See DEFAULT_BUDGET_USD.
    budget_usd: float | None = DEFAULT_BUDGET_USD
    feature_budget_usd: float | None = DEFAULT_FEATURE_BUDGET_USD


# The validation panel's default roster: every seat in the vocabulary. Unlike Neo's
# DEFAULT_ROSTER, which is short because most of its seats have no definition shipped
# yet, this one names all five — the panel's whole point is the four independent lenses
# plus a chair, and a default that quietly dropped one would remove a check and tell
# nobody. A seat whose markdown has not shipped yet still PARSES (see
# `_parse_validation`); it fails loudly at run time instead of refusing to boot.
DEFAULT_VALIDATION_ROSTER = VALIDATOR_SEATS

# Per-seat call timeout, seconds. Longer than the Neo panel's 120: a validator seat
# reads a diff of up to `diff_chars` before it says anything.
DEFAULT_VALIDATION_TIMEOUT = 300

# How many times a unit may be sent back before the loop gives up and asks a human.
DEFAULT_VALIDATION_MAX_ROUNDS = 3

# Truncation limit for the diff a seat is shown. MEASURED, not chosen: at 0.384
# tokens/char a round's shared prefix is 76,347 tokens here, which under the shared-cache
# layout costs less than HALF what 60,000 cost when every seat wrote its own copy
# (125,973 against 261,388 input-equivalent tokens). 300,000 is affordable too and is
# refused on CONTEXT — 137,621 prefix tokens leaves too little of a 200k window for the
# packet's other sections and the seat's own reasoning, and a seat that overflows
# abstains. PR #206, the change every seat of wo-a6af01f0 complained it could not read,
# is 150,380 chars. Spec §6:
# docs/superpowers/specs/2026-09-13-a-round-the-panel-can-afford.md
DEFAULT_VALIDATION_DIFF_CHARS = 150000

# How many follow-up findings one round may file against the project backlog.
#
# A BOUND, NOT A JUDGEMENT. Title matching is the only dedupe available — a seat writes a
# slightly different sentence for the same nit each round — so near-duplicate rows WILL
# get through. 5 keeps that failure a handful of items a user drops in a minute rather
# than a backlog nobody can read; findings past it are dropped and a later round may
# raise them again. Spec §4.4:
# docs/superpowers/specs/2026-09-15-the-panel-blocks-on-blockers.md
DEFAULT_VALIDATION_FOLLOW_UP_CAP = 5


@dataclass
class ValidationConfig:
    """An independent panel judging a work order's or feature order's claim of done.

    SHIPS DISABLED, and that is a requirement rather than caution: at this default the
    OS must behave exactly as it does today — same statuses reached, same events, same
    number of `claude` calls, and not one row in `validation_rounds`. A round is roughly
    five headless calls over a diff of up to `diff_chars`, up to `max_rounds` times, on
    every unit in the fleet, so enabling it is a catalog edit gated on a measurement.

    THAT RULE HAS ONE RULED CARVE-OUT (Neo, question 309 on wo-38e26be0, 2026-09-15;
    kn-a88a56b6). It governs a field that ADDS panel behaviour, where the default decides
    whether the behaviour happens at all. It does not govern a field whose behaviour change
    is already unconditional elsewhere in the same feature: where the panel's rejection
    semantics have already changed with no knob, a field that only chooses between
    preserving the finding as a ticket and discarding it silently is not gated on a
    measurement, because the measurement — do the seats classify blocking vs non-blocking
    correctly — is taken by the eval regardless of the field's value. Such a field may ship
    default True. Named instance: the follow-up-filing field from the 2026-09-15
    panel-blocks-on-blockers feature
    (docs/superpowers/specs/2026-09-15-the-panel-blocks-on-blockers.md §5.4).

    `seat_models` and `chair_model` are empty by default, meaning "use the project's
    model"; the fallback is resolved where it is used, not here, for the same reason
    `PanelConfig` does it — a nested dataclass cannot see its parent's fields.
    """

    enabled: bool = False
    roster: tuple[str, ...] = DEFAULT_VALIDATION_ROSTER
    seat_models: dict[str, str] = field(default_factory=dict)
    chair_model: str = ""
    timeout: int = DEFAULT_VALIDATION_TIMEOUT
    max_rounds: int = DEFAULT_VALIDATION_MAX_ROUNDS
    diff_chars: int = DEFAULT_VALIDATION_DIFF_CHARS
    # Whether a FEATURE order validates as a whole once its children are done, which is
    # a separate question from whether its children each validated: the feature is the
    # only level at which "does this add up to what was asked" can be judged.
    feature_units: bool = True
    # WHETHER THE OS MAY MERGE THIS PROJECT'S PULL REQUESTS ITSELF, once the panel has
    # accepted the exact commit at the head and CI is green
    # (docs/superpowers/specs/2026-09-14-validated-auto-merge-design.md).
    #
    # PER PROJECT, and the inheritance is this block's ordinary field-level fallback: a
    # project that names a value keeps it, a project that does not takes the OS answer —
    # the same shape as every `inspect.alarm_*` threshold. So authority is granted one
    # project at a time, and a project that has never opted in is never merged by the OS
    # however the fleet is configured.
    #
    # SHIPS FALSE at both levels. This is not caution about a setting, it is the only
    # thing in the catalog that decides whether the OS holds merge authority at all, and
    # `*.validation.*` already puts it behind `jarvis config set`'s mandatory `--reason`
    # and a recorded config version. It is ALSO read beside `enabled`, never instead of
    # it: turning the panel off stops auto-merge with it, because the acceptance this
    # rides on is the panel's.
    auto_merge: bool = False
    # WHETHER THE OS MAY DECIDE THIS PROJECT'S PENDING ASSUMPTIONS ITSELF, instead of
    # holding the work order until the user rules on each one
    # (docs/superpowers/specs/2026-09-15-neo-decides-an-assumption.md).
    #
    # The same shape as `auto_merge` above in every respect — per project, ordinary
    # field-level fallback, `*.validation.*` so `jarvis config set` demands a reason and
    # stamps a config version, ships FALSE at both levels — and for the same reason: it
    # is an authority the user held, handed to the OS one project at a time.
    #
    # Read BESIDE `enabled`, never instead of it. The reviewer this rides on is Neo, but
    # what makes an accepted assumption safe to land is that the panel judged the work it
    # was part of; turning the panel off must stop this with it.
    auto_review: bool = False
    # WHETHER A NON-BLOCKING FINDING IS KEPT AS A BACKLOG ITEM, or discarded. `False`
    # does not restore the old behaviour — the panel stopped rejecting over one with no
    # knob at all — it only throws the finding away, so this is the named instance of the
    # carve-out above and it ships True. `kn-a88a56b6` is the discriminator; do not
    # re-decide it here. Spec §4.5:
    # docs/superpowers/specs/2026-09-15-the-panel-blocks-on-blockers.md
    #
    # Per project by the same field-level fallback as `auto_merge` and `auto_review`.
    follow_ups: bool = True
    # The cap above, per project. See DEFAULT_VALIDATION_FOLLOW_UP_CAP.
    max_follow_ups: int = DEFAULT_VALIDATION_FOLLOW_UP_CAP


# -- the house style: how terse the OS holds its workers ---------------------------------


@dataclass(frozen=True)
class ConcisionConfig:
    """The one number the concision machinery needs per project.

    Design: docs/superpowers/specs/2026-09-19-concision-enforced.md SS5.3. Per project
    as well as fleet-wide, with `_parse_inspect`'s field-level inheritance.

    `summary_max_words` = 0 turns the `jarvis wo finish --summary` refusal OFF for the
    project, which is why zero is ACCEPTED here where `_parse_inspect` refuses it. The
    two mean opposite things: an inspect count of zero would make an alarm fire on
    everything, whereas a cap of zero makes a refusal fire on nothing. Off is a position
    a project is entitled to take; an alarm that cries wolf is not.
    """

    summary_max_words: int = concision.DEFAULT_SUMMARY_MAX_WORDS


# -- `jarvis inspect`: what counts as worth reporting, and what as worth interrupting for
#
# EVERY NUMBER HERE WAS MEASURED, over the 438 worker turns and 118 dispatched work orders
# on this machine at the time it shipped, and none of them is a round number chosen
# because it looked reasonable. The measured firing rate is stated beside each one,
# because that rate is what a person is really choosing when they change it. Method and
# figures: docs/superpowers/specs/2026-08-30-the-anatomy-of-a-turn.md §6.
#
# The `report_` pair only decide what `jarvis inspect` PRINTS. The `alarm_` trio decide
# when the OS interrupts the user about a turn that is still running, which is a much
# higher bar — hence 300s against 30s for the same blocking join, and 300k against 20k for
# the same cache write. They are prefixed rather than nested so that a reader of
# `jarvis config show` cannot mistake one for the other; confusing the two would either
# flood the attention list or empty the report.

#: A cache write below this is the conversation's own growth rather than a re-send of it,
#: and labelling it by the gap alone would call the cache working a defect. The method's
#: own figure (`docs/findings/anatomy-of-an-expensive-turn.md` §1 step 5: "flag every
#: write over ~20k"), kept because it is what the worked example was derived with.
DEFAULT_INSPECT_REPORT_WRITE_FLOOR = 20_000

#: A blocking join shorter than this is not worth a line of its own. Well under the
#: alarm's threshold on purpose: the report is read deliberately and can afford detail
#: the attention list cannot.
DEFAULT_INSPECT_REPORT_JOIN_FLOOR = 30

#: How much of a turn's triggering prompt is quoted. A terminal line, and the quote is
#: there to identify the prompt rather than to reproduce it.
DEFAULT_INSPECT_QUOTE_CHARS = 140

#: A turn still running after this long is burning money now.
#:
#: MEASURED IN ACTIVE TIME, NOT WALL CLOCK (`holds`, and the user's ruling of
#: 2026-09-18): wall clock minus every interval the OS's own record says the order was
#: held. A turn that spans a spent usage window is not slow, it is obeying, and an alarm
#: that cannot tell those apart teaches the user to ignore alarms.
#:
#: SIXTY SURVIVES THE CHANGE OF CLOCK, and was re-measured rather than carried over. Over
#: this fleet's 674 transcript turns on 2026-09-18 (307 of them held for something), an
#: hour fires on 22.7% of turns by wall clock and 13.8% by active time — so moving the
#: denominator removes two of every five alarms without touching a turn that genuinely
#: worked for an hour. Raising it as well would silence the ones the user does want: the
#: false alarms were the held ones, and they are gone by construction now. It still sits
#: well below `worker_session.TURN_STALL_SECONDS` (6h), which reports a different fact:
#: hung, not expensive.
DEFAULT_INSPECT_ALARM_TURN_MINUTES = 60

#: A turn that has been open this long WITHOUT MAKING A SINGLE API CALL. Not a spend
#: threshold at all — its finding is that nothing was spent, and it is the detector issue
#: 227 asked for after a 65-minute dead turn was reported as 65 minutes of generation.
#: MEASURED over the 4,543 worker turns on this machine: the p99 time from a turn opening
#: to its first API call is 61 seconds and exactly ONE turn in 3,811 took longer than ten
#: minutes, so fifteen is far outside a slow start; it fires on 12 turns, 0.26%. Below
#: `DEFAULT_INSPECT_ALARM_TURN_MINUTES` on purpose — a stall must be named a stall before
#: the long-turn alarm's hour is up, or the first thing the user hears about it is a claim
#: about money.
#:
#: ALSO IN ACTIVE TIME now, for the reason above it: "the work never started" is a claim
#: about the work, and a turn the OS was holding had not been allowed to start one. The
#: change only makes this stricter, so the number stands — re-measured on 2026-09-18 over
#: the same 674 turns, the p99 time to first call is 80 seconds and the worst is 177, so
#: fifteen minutes is still two orders of magnitude outside a slow start.
DEFAULT_INSPECT_ALARM_STALLED_MINUTES = 15

#: A blocking join still open after this long. THE ONLY THRESHOLD HERE THAT IS PRINCIPLED
#: RATHER THAN EMPIRICAL: it is the 5-minute cache TTL itself, past which the prefix is
#: certainly cold and the wait will be paid for a second time as a re-write. Fires on 2%.
#:
#: STAYS ON THE WALL CLOCK while the two above moved to active time, and that is the
#: whole point of deciding per threshold rather than sweeping the file. What this
#: measures is a cache entry ageing out, and the entry expires in real seconds whether
#: or not the OS was allowing the order to work. It is also a JOIN — the lead agent
#: waiting on its own subagent — which is the order's own choice and the one wait the
#: user's ruling explicitly kept on the books.
DEFAULT_INSPECT_ALARM_JOIN_SECONDS = 300

#: One call re-sending this much of the conversation. p95 of the largest re-write per work
#: order (the median is 130,519), so it fires on 5% — about $1.88 at Opus list prices in a
#: single event.
DEFAULT_INSPECT_ALARM_WRITE_TOKENS = 300_000

#: How long a work order may sit with its turn ended and nothing in flight before
#: `invariants.parked_reason` calls it parked. THE ONE THRESHOLD HERE THAT IS NOT ABOUT
#: SPEND: the other three ask "is this costing too much", this one asks "is anything
#: going to happen to this at all". An hour rather than minutes because the ordinary
#: settlement path moves a finished turn within one reconcile tick, so anything still
#: sitting here an hour later is not slow, it is stopped — wo-a4bd6958 sat 7h13m.
#:
#: IN ACTIVE TIME TOO, which for a stopped order means "an hour in which nothing was
#: holding it". `parked_reason` already refused to flag most held orders, but by an
#: inventory (`SPOKEN_FOR_WAITS`) rather than by a clock — so a hold with no matching
#: entry in that tuple still cost the user a line. Subtracting the recorded holds makes
#: the refusal follow from the measurement instead of from a list someone has to keep
#: complete. The hour is unchanged: it was always a claim about how long a silence has
#: to run before it means something, and taking the OS's own waiting out of that silence
#: only makes the claim truer.
DEFAULT_INSPECT_ALARM_PARKED_MINUTES = 60

# -- the AGGREGATE half of the re-write tax. Everything above judges one live turn;
# these five judge a PROJECT over a cohort window of settled orders, which is the
# standing condition no surface raised (issue 164 item 1, finding 1 of
# docs/superpowers/findings/2026-08-30-where-the-800-dollars-went.md).
#
# NONE OF THESE FIVE — NOR THE TWO `cache_1h` ONES BELOW — MOVES TO ACTIVE TIME, and that
# is a decision rather than an oversight. Every one of them judges a count of TOKENS or a
# share of a BILL; there is no duration in any numerator for a hold to come out of. The
# `_window_days` figures are calendar time and stay calendar time: they choose which
# settled orders are in the cohort, and an order that spent four hours held was still
# settled last Tuesday.

#: The cohort window, in days. NOT ALL HISTORY, and that is the point (kn-1449447a (5)):
#: the split between the two causes is drifting, and an average over everything ever
#: sealed reports the trend away.
DEFAULT_INSPECT_ALARM_REWRITE_WINDOW_DAYS = 7

#: What fraction of the window's bill went on re-sending conversations whose PROMPT
#: PREFIX had moved — the cause no cache TTL can touch. MEASURED over this fleet's sealed
#: bills on 2026-09-14: jarvis_os sits at 10.5% over the trailing 7 days and 11.1% over
#: 30. Set above that deliberately, so the alarm reports the tax GETTING WORSE rather than
#: restating a standing figure the findings doc already recorded; a project that wants the
#: standing figure raised lowers it with `jarvis config set <p>
#: inspect.alarm_rewrite_prefix_share`.
DEFAULT_INSPECT_ALARM_REWRITE_PREFIX_SHARE = 0.15

#: The same fraction for the other cause: the cache entry EXPIRING. Measured at 10.3% (7
#: days) and 10.9% (30) on the same run, so the two causes are at near parity on this
#: fleet now — itself the drift kn-1449447a (5) predicted.
#:
#: THIS IS NOT THE TRIGGER FOR SWITCHING THE WRITE TTL, and confusing the two is the trap
#: kn-1449447a (4) exists for: that decision is `rewrite_ttl_write / cache_write` against
#: 39.5%, a different ratio over a much larger denominator, and it runs at about half this
#: one. This is a share of the BILL.
DEFAULT_INSPECT_ALARM_REWRITE_TTL_SHARE = 0.15

#: A share computed over fewer settled orders than this is one order's shape rather than
#: the project's. Measured: it excludes openclaw_sandbox's single $28.26 order sitting at
#: 31.8%, which is a work order to look at and not a project to alarm on.
DEFAULT_INSPECT_ALARM_REWRITE_MIN_ORDERS = 5

#: …and below this much spend in the window a percentage is arithmetic on noise. Measured:
#: it excludes shared_schedule ($0.54 over 3 orders) and painforwisdom ($2.32 over 4)
#: without excluding anything that had money in it. Zero is legal — see
#: `INSPECT_MONEY_KEYS`.
DEFAULT_INSPECT_ALARM_REWRITE_MIN_USD = 10.0

# -- who is still buying the ONE-HOUR cache write. The two above judge a project's own
# sealed bills; these two judge the TRANSCRIPT TREE, including sessions the OS never
# dispatched and has no other record of (issue 164 item 3, finding 3 of
# docs/superpowers/findings/2026-08-30-where-the-800-dollars-went.md).

#: The window, in days. Seven for `DEFAULT_INSPECT_ALARM_REWRITE_WINDOW_DAYS`' reason and
#: one of its own: the condition this detects is a SETTING going missing — a fresh
#: machine, a new shell profile — and a week is short enough that the leak is named while
#: the change that caused it is still recent.
DEFAULT_INSPECT_ALARM_CACHE_1H_WINDOW_DAYS = 7

#: One-hour tokens written in the window by sessions JARVIS DID NOT DISPATCH. THIS IS A
#: LEAK DETECTOR AND NOT A HEADLINE SAVING, and the threshold says so: the 1h premium on
#: 500k tokens is ~$1.88 at Opus list, so this is not sized to catch money, it is sized to
#: catch the setting disappearing before a year of it adds up. MEASURED on this machine
#: 2026-09-16: zero 1h tokens in the trailing 7 and 14 days (the user's own
#: `FORCE_PROMPT_CACHING_5M` is holding), against ~3.8M/week in the ten days before it was
#: set — so this fires within a week of the line being lost and never on a quiet one. Set
#: above the median offending session (87k) so one afternoon's hand-opened `claude` is not
#: an alarm.
DEFAULT_INSPECT_ALARM_CACHE_1H_TOKENS = 500_000

#: The same, for turns JARVIS ITSELF dispatched — a BREACH of `claude_cli.cache_env`, and
#: a defect in this OS rather than in anyone's personal config. An order of magnitude
#: lower because it is a different finding, not a smaller one: a worker turn writes
#: 100-300k at a boundary, so this catches a SINGLE breached turn while staying above a
#: stray row. The last one the fleet had was 299,610 tokens over 2026-08-22/23, before
#: the transport fix reached production.
DEFAULT_INSPECT_ALARM_CACHE_1H_DISPATCHED_TOKENS = 50_000

#: `InspectConfig` fields that are a FRACTION, not a count. `_parse_inspect` refuses every
#: other field below 1, which would reject every legal value of these two.
INSPECT_FRACTION_KEYS = ("alarm_rewrite_prefix_share", "alarm_rewrite_ttl_share")

#: …and the one that is money. Zero is legal here and nowhere else: a project may ask to
#: hear about its re-write tax however little it spent.
INSPECT_MONEY_KEYS = ("alarm_rewrite_min_usd",)


@dataclass
class InspectConfig:
    """What `jarvis inspect` reports, and when the OS raises a turn that is still running.

    Per project as well as fleet-wide, with field-level inheritance (`_parse_inspect`):
    a project that names one key keeps the OS answer for the rest. That matters because
    the alarm thresholds are a statement about what is NORMAL, and normal differs by
    project — an hour-long turn is routine where the work is a design document and a
    symptom where it is a one-file fix.

    `enabled` turns only the ALARMS off, never the report: `jarvis inspect` reads files
    that are already on disk and costs nothing until someone runs it, whereas the alarm
    reads a transcript per running work order per reconcile tick. It covers
    `alarm_parked_minutes` too (`invariants._parked_minutes`) and the `alarm_rewrite_`
    five (`Daemon.check_rewrite_tax`) — one switch for "raise nothing here", because a
    second way to turn one thing off is a second way to be surprised by it.
    """

    enabled: bool = True
    report_write_floor: int = DEFAULT_INSPECT_REPORT_WRITE_FLOOR
    report_join_floor: int = DEFAULT_INSPECT_REPORT_JOIN_FLOOR
    quote_chars: int = DEFAULT_INSPECT_QUOTE_CHARS
    alarm_turn_minutes: int = DEFAULT_INSPECT_ALARM_TURN_MINUTES
    alarm_stalled_minutes: int = DEFAULT_INSPECT_ALARM_STALLED_MINUTES
    alarm_join_seconds: int = DEFAULT_INSPECT_ALARM_JOIN_SECONDS
    alarm_write_tokens: int = DEFAULT_INSPECT_ALARM_WRITE_TOKENS
    alarm_parked_minutes: int = DEFAULT_INSPECT_ALARM_PARKED_MINUTES
    alarm_rewrite_window_days: int = DEFAULT_INSPECT_ALARM_REWRITE_WINDOW_DAYS
    alarm_rewrite_prefix_share: float = DEFAULT_INSPECT_ALARM_REWRITE_PREFIX_SHARE
    alarm_rewrite_ttl_share: float = DEFAULT_INSPECT_ALARM_REWRITE_TTL_SHARE
    alarm_rewrite_min_orders: int = DEFAULT_INSPECT_ALARM_REWRITE_MIN_ORDERS
    alarm_rewrite_min_usd: float = DEFAULT_INSPECT_ALARM_REWRITE_MIN_USD
    alarm_cache_1h_window_days: int = DEFAULT_INSPECT_ALARM_CACHE_1H_WINDOW_DAYS
    alarm_cache_1h_tokens: int = DEFAULT_INSPECT_ALARM_CACHE_1H_TOKENS
    alarm_cache_1h_dispatched_tokens: int = \
        DEFAULT_INSPECT_ALARM_CACHE_1H_DISPATCHED_TOKENS


# -- message delivery: how long a queued message may stay undelivered before the OS
# calls it stuck. A threshold a surface judges by, so it is a setting and not a module
# constant (kn-67cdb54b), and ABOVE `ProjectSpec` for the `field(default_factory=…)`
# reason kn-6ca2bcd9 gives.

#: `Daemon.deliver_messages` sends within one reconcile tick, so this is not a
#: latency bar — it is the point past which every ACCOUNTED wait has been excused by
#: `invariants.stuck_message` and what is left is a message the worker will never see.
#: An hour rather than minutes for `DEFAULT_INSPECT_ALARM_PARKED_MINUTES`' reason: the
#: one unaccounted wait that is legitimately long is a project whose `max_concurrent`
#: slots are all full, and flagging that after minutes would put a line on the
#: attention list for a fleet that is merely busy.
DEFAULT_MESSAGING_STUCK_MINUTES = 60


# -- the bug lifecycle: what happens to a GitHub issue `jarvis bug report` files. Issue
# #240, and `docs/superpowers/specs/2026-09-14-a-filed-bug-runs-itself.md`. Per project
# with a fleet fallback, on `_parse_inspect`'s shape, and the project it is read from is
# THE ONE THAT WOULD DO THE WORK — never the one that noticed the bug. Any agent in the
# fleet can run `jarvis bug report`, so the project that pays for the work is the only
# one whose consent means anything.

#: The label that says the OS has picked an issue up. Wording from issue #240 itself.
DEFAULT_BUGS_LABEL = "in progress"


@dataclass
class BugsConfig:
    """How the tracker is labelled while the OS works on a bug it filed.

    WHAT IS NOT HERE IS THE POINT. There is no `auto_work_order` switch: routing is by
    priority and only by priority (the user's ruling of 2026-09-14 settling issue #240's
    decision A), and what stops a filing committing the fleet to work is the rubric
    (`issues.PRIORITY_RUBRIC`) plus Neo re-assessing every `critical`/`blocker` claim —
    not a catalog key. A key that shipped off would have made that ruling's own
    "critical and blocker automatically get a work order" unreachable.

    `label` is the whole of the in-progress signal: nothing derives it, and an issue
    carries it exactly while a live work order is on it (`issues.desired_state`). The
    priority labels beside it are not configurable — they are the vocabulary itself
    (`issues.PRIORITY_LABELS`), and a fleet that renamed them would have a tracker its
    own rubric no longer describes.
    """

    label: str = DEFAULT_BUGS_LABEL


@dataclass
class MessagingConfig:
    """When a message queued for a worker stops being in flight and becomes a defect.

    Per project as well as fleet-wide, with the field-level inheritance `_parse_inspect`
    uses, and for the same reason: "how long is too long" is a claim about what is
    normal, and a project that runs one work order at a time queues behind itself far
    more than one that runs five.
    """

    stuck_minutes: int = DEFAULT_MESSAGING_STUCK_MINUTES


# -- the scheduler: the OS filing a work order nobody typed. ABOVE `ProjectSpec` for the
# `field(default_factory=…)` reason kn-6ca2bcd9 gives, and a catalog setting rather than a
# module constant for kn-67cdb54b's: "how often, and which jobs" is policy the user holds
# an opinion about. Reasoning: docs/superpowers/specs/2026-09-14-the-scheduler.md §2.

#: Daily, which is the cadence the ask named. Hours rather than a cron expression on
#: purpose: a cron string can express "every minute", and the first mechanism that spends
#: money on its own should not be one typo away from doing so.
DEFAULT_SCHEDULE_INTERVAL_HOURS = 24

#: How many whole intervals a job may sit HELD — wanting to fire, blocked behind its own
#: unsettled previous order — before `invariants.check_schedule_progresses` reports it.
#: Three rather than one: a doctor order the user has not got round to reviewing is a
#: normal Tuesday, and flagging that would put the scheduler itself on the attention list
#: for the crime of working. Three days of silence is a scheduler that has stopped.
DEFAULT_SCHEDULE_HELD_ALARM_INTERVALS = 3


@dataclass
class WiringConfig:
    """Which of the USER'S OWN MCP servers, skills and plugins reach this project's
    workers. Read `wiring.py` for the levers; spec
    docs/superpowers/specs/2026-09-16-per-project-wiring.md §2 for why they are these.

    OPT-OUT, and that is the whole shape (user ruling, 2026-09-16): every default here
    means "wired", so a project that never opens /config launches exactly the session it
    launched before this block existed, and a server the user installs next month is
    wired everywhere without a catalog edit. What a project stores is its DEVIATION.

    Mixed polarity is deliberate and the page hides it: the two flags name a block
    nothing can subdivide (the claude.ai connectors move as one, and Claude Code's
    bundled skills likewise), while the two lists name individuals. Both render as one
    checkbox per row, so "wired" is the only word a reader meets.

    The lists REPLACE rather than merge when a project overrides them — `ScheduleConfig.
    jobs`' rule (kn-6ca2bcd9), for its reason: inheritance is field-level.
    """

    claude_ai_connectors: bool = True
    bundled_skills: bool = True
    #: `<plugin>@<marketplace>` ids, as `claude plugin list` spells them.
    disabled_plugins: tuple[str, ...] = ()
    #: Skill names — user and project skills only. A PLUGIN's skills follow their
    #: plugin: Claude Code ignores `skillOverrides` for a plugin-sourced skill.
    disabled_skills: tuple[str, ...] = ()


@dataclass
class ScheduleConfig:
    """Recurring work orders: whether, how often, and which.

    TWO SWITCHES RATHER THAN ONE, `RemedyConfig`'s pattern and for a sharper reason: this
    is the only thing in the OS that files work — and therefore spends the user's money —
    with nobody asking. `enabled` ships FALSE, so a fleet that upgrades into this feature
    schedules nothing until somebody says so, while `jobs` still carries a meaningful
    default roster so turning it on is one setting and not two.

    `jobs` names ids of `schedule.JOBS`; an unknown one is a `CatalogError` naming the
    known ids (`GateConfig.parse`'s rule — a typo must not silently leave a job unset).
    It REPLACES rather than merges when a project overrides it, for the reason
    `seat_models` does (kn-6ca2bcd9): inheritance is field-level and this is one field.

    Per project as well as fleet-wide, with `_parse_inspect`'s field-level inheritance:
    "should this project be swept daily" is exactly the kind of claim that differs by
    project — an OS checkout wants it, an archived repo does not.
    """

    enabled: bool = False
    interval_hours: int = DEFAULT_SCHEDULE_INTERVAL_HOURS
    jobs: tuple[str, ...] = schedule_mod.JOB_IDS
    held_alarm_intervals: int = DEFAULT_SCHEDULE_HELD_ALARM_INTERVALS

    @property
    def interval_seconds(self) -> float:
        return self.interval_hours * schedule_mod.SECONDS_PER_HOUR


# -- the supervisor: it JUDGES a cost alarm, so every number it judges by is a setting
# rather than a module constant (kn-67cdb54b). Reasoning, and why none of it belongs in
# `InspectConfig`: docs/superpowers/specs/2026-08-31-the-supervisor.md §2.
#
# ABOVE `ProjectSpec`, which uses it via `field(default_factory=…)` — below it is a
# NameError at import (kn-6ca2bcd9).

DEFAULT_SUPERVISOR_MODEL = "opus"
DEFAULT_SUPERVISOR_TIMEOUT = 300
DEFAULT_SUPERVISOR_LEARNINGS_LIMIT = 50

#: Past this an alarm is skipped unjudged: spend the user can no longer prevent.
DEFAULT_SUPERVISOR_MAX_AGE_HOURS = 24

#: MUST EXCEED `timeout`, or a claim is reclaimed out from under a call that is still
#: running and one alarm is judged twice. `_parse_supervisor` refuses a catalog that
#: breaks the relation.
DEFAULT_SUPERVISOR_STALE_REVIEWING_SECONDS = 900
DEFAULT_SUPERVISOR_MAX_REVIEW_ATTEMPTS = 3

#: The evidence packet's ceiling and the clips that fill it. `conversation_` and
#: `description_` are prefixed because both measure quoted characters (kn-67cdb54b).
DEFAULT_SUPERVISOR_EVIDENCE_BUDGET_CHARS = 8000
DEFAULT_SUPERVISOR_CONVERSATION_QUOTE_CHARS = 400
DEFAULT_SUPERVISOR_QUOTED_TURNS = 3
DEFAULT_SUPERVISOR_DESCRIPTION_CHARS = 500

#: What the user is told, and how much of an unusable reply is kept as the reason.
DEFAULT_SUPERVISOR_NOTE_CHARS = 200
DEFAULT_SUPERVISOR_REASON_CHARS = 200

# -- the health sweep's numbers, created here so §4 touches no catalog code and the two
# can be reviewed apart. NOTHING IN THIS RELEASE READS THEM:
# docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md §2 ships the list
# and §4 ships the trigger that reads it.

#: How often the sweep runs, and the floor between two looks at the SAME unit — a unit
#: nothing has happened to is not judged twice.
DEFAULT_SUPERVISOR_HEALTH_EVERY_TICKS = 20
DEFAULT_SUPERVISOR_HEALTH_MIN_INTERVAL_MINUTES = 30

#: How long a unit's fingerprint may sit still before that stillness is itself the thing
#: worth looking at (§4's `stale` clause).
DEFAULT_SUPERVISOR_HEALTH_STALE_MINUTES = 720

#: The per-tick ceiling: the sweep is the standing cost of watching, so it is bounded
#: before it is enabled rather than after a bill arrives.
DEFAULT_SUPERVISOR_HEALTH_MAX_UNITS_PER_TICK = 4

#: Every armed probe rides in ONE system prompt, so both of these bound that prompt.
DEFAULT_SUPERVISOR_MAX_ENABLED_PROBES = 12
DEFAULT_SUPERVISOR_PROBE_PROMPT_CHARS = 800


@dataclass(frozen=True)
class RemedyConfig:
    """What the supervisor is permitted to PROPOSE doing about a unit it judged ill —
    docs/superpowers/specs/2026-09-02-supervisor-health-and-healing.md §5.

    TWO SWITCHES RATHER THAN ONE, DELIBERATELY. `enabled: true` with `allowed: []` means
    the supervisor may reason about remedies and nothing is ever applied — a genuinely
    useful shipping state, and the one the fleet should run first. Collapsing them into
    a single flag would make "let me watch it want to act" unexpressible.

    Ships with both off. `allowed` names ids of `remedies.REMEDIES`; an unknown one is a
    `CatalogError` naming the known ids, `GateConfig.parse`'s rule and for its reason — a
    typo must not silently leave a permission unset.

    Defined ABOVE `SupervisorConfig` because it is reached through
    `field(default_factory=...)`, which is a `NameError` at import the other way round
    (kn-67cdb54b).
    """

    enabled: bool = False
    allowed: tuple[str, ...] = ()


@dataclass
class SupervisorConfig:
    """The agent that reviews a cost alarm and either acks it or wants Neo — §2.

    SHIPS DISABLED: a wrong ack makes a burning turn invisible, which is a strict
    regression on what PR 159 shipped. Field-level per-project inheritance
    (`_parse_supervisor`), as `_parse_inspect` does it.
    """

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

    # The health sweep. `probes` is a whole immutable list rather than a scalar, so
    # `jarvis config set <p> supervisor.probes` addresses all of it at once and a single
    # probe is not editable from the console — a catalog file edit is the intended
    # route (§2), and bl-8e47dd53 tracks the console form.
    probes: tuple[probes_mod.HealthProbe, ...] = probes_mod.DEFAULT_PROBES
    health_enabled: bool = False
    health_every_ticks: int = DEFAULT_SUPERVISOR_HEALTH_EVERY_TICKS
    health_min_interval_minutes: int = DEFAULT_SUPERVISOR_HEALTH_MIN_INTERVAL_MINUTES
    health_stale_minutes: int = DEFAULT_SUPERVISOR_HEALTH_STALE_MINUTES
    health_max_units_per_tick: int = DEFAULT_SUPERVISOR_HEALTH_MAX_UNITS_PER_TICK
    max_enabled_probes: int = DEFAULT_SUPERVISOR_MAX_ENABLED_PROBES
    probe_prompt_chars: int = DEFAULT_SUPERVISOR_PROBE_PROMPT_CHARS

    # What it may PROPOSE doing about what it finds (§5). A whole block rather than a
    # scalar, for `probes`' reason: `jarvis config set <p> supervisor.remedies.allowed`
    # addresses the permission as one list.
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
    # Privileged actions this project's workers may attempt under review rather than
    # not at all (see gates.py). Off by default: enabling a gate widens what a worker
    # can do, so it is always a deliberate per-project choice.
    gates: GateConfig = field(default_factory=GateConfig)
    # Already RESOLVED against `os.validation`: `_parse_validation` is handed the OS
    # config as its base, so every field here is the answer for this project and no
    # caller has to consult two objects. See
    # docs/superpowers/specs/2026-08-27-the-config-console.md §1.2.
    validation: ValidationConfig = field(default_factory=ValidationConfig)
    inspect: InspectConfig = field(default_factory=InspectConfig)
    concision: ConcisionConfig = field(default_factory=ConcisionConfig)
    supervisor: SupervisorConfig = field(default_factory=SupervisorConfig)
    messaging: MessagingConfig = field(default_factory=MessagingConfig)
    bugs: BugsConfig = field(default_factory=BugsConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    wiring: WiringConfig = field(default_factory=WiringConfig)
    raw: dict[str, Any] = field(default_factory=dict)


# The seats the panel runs by default. Deliberately NOT `neo_store.SEATS`: `record`,
# `blast` and `taste` have no definition shipped yet, and a default roster naming safety
# seats that cannot run would be worse than a short one that says what it is.
DEFAULT_ROSTER = ("premise", "chair")

# Which question kinds the panel may answer by default. `plan` is excluded on purpose: a
# feature order's plan review has its own reviewed persona (`plans.PLAN_REVIEWER_PERSONA`)
# that the seats' mandates say nothing about, so including it would silently swap a
# reviewed persona for one written for a different job on the day someone enables this.
DEFAULT_PANEL_KINDS = ("question", "approval")

# Per-seat call timeout. Well below Neo's own 300s: the seats run concurrently but inside
# the daemon's single Neo thread, so the whole FIFO drain — and every worker parked behind
# it — waits on the slowest seat.
DEFAULT_PANEL_TIMEOUT = 120


@dataclass
class PanelConfig:
    """Neo answering as a panel of profiled seats instead of as one agent.

    SHIPS DISABLED, and that is a requirement rather than caution: at this default the
    OS's behaviour must be byte-identical to the single-agent path — same number of
    Claude calls, same system prompt, same message to the worker. Enabling it is a
    catalog edit, gated on a measurement that does not exist yet.

    `seat_models` and `chair_model` are both empty by default, meaning "use
    `NeoConfig.model`"; a nested dataclass cannot see its parent's field at construction,
    so the fallback is resolved where it is used (`panel.seat_model`).
    """

    enabled: bool = False
    roster: tuple[str, ...] = DEFAULT_ROSTER
    seat_models: dict[str, str] = field(default_factory=dict)
    chair_model: str = ""
    timeout: int = DEFAULT_PANEL_TIMEOUT
    kinds: tuple[str, ...] = DEFAULT_PANEL_KINDS
    fast_path: bool = True


@dataclass
class NeoConfig:
    """Neo, the OS answerer agent (responds to worker questions as the user)."""
    enabled: bool = True
    model: str = "opus"
    learnings_limit: int = 50
    # NOTE: parsed and never read — Neo's calls take `run_headless`'s own 300s default.
    # Filed as bl-9a925d2e. Do not quietly start honouring it: a knob nobody could use
    # that suddenly bites changes live Neo behaviour under cover of an unrelated change.
    timeout: int = 300
    # Which model shortens an over-long question for the dashboard (`jarvis.digest`).
    # A cheap one on purpose: the digest is display-only — it never reaches Neo, a
    # worker or a learning — so this is a formatting job, not a judgement one.
    # SET IT TO "" TO TURN DIGESTING OFF: no model named, no call made, and the page
    # falls back to rendering every question in full, which is what it did before.
    digest_model: str = "haiku"
    panel: PanelConfig = field(default_factory=PanelConfig)


@dataclass
class OsConfig:
    default_model: str = DEFAULT_MODEL
    default_effort: str | None = None
    default_permission_mode: str = DEFAULT_PERMISSION_MODE
    default_max_concurrent: int = DEFAULT_MAX_CONCURRENT
    #: Fleet-wide worker turns in flight. No `ProjectSpec` twin on purpose — see
    #: DEFAULT_MAX_IN_FLIGHT.
    max_in_flight: int = DEFAULT_MAX_IN_FLIGHT
    default_autocompact_window: int | None = DEFAULT_AUTOCOMPACT_WINDOW
    default_budget_usd: float | None = DEFAULT_BUDGET_USD
    default_feature_budget_usd: float | None = DEFAULT_FEATURE_BUDGET_USD
    #: Read by the cost surfaces, not by a worker launch — see DEFAULT_COLD_PREFIX_FLOOR.
    cold_prefix_floor: int = DEFAULT_COLD_PREFIX_FLOOR
    cold_prefix_floor_max: int = DEFAULT_COLD_PREFIX_FLOOR_MAX
    #: Read by the two `jarvis doctor` cache-health post-conditions, which judge the
    #: fleet and not a project — see DEFAULT_CACHE_HEALTH_WINDOW_DAYS.
    cache_health_window_days: int = DEFAULT_CACHE_HEALTH_WINDOW_DAYS
    cache_health_min_orders: int = DEFAULT_CACHE_HEALTH_MIN_ORDERS
    cache_health_min_boundaries: int = DEFAULT_CACHE_HEALTH_MIN_BOUNDARIES
    cache_health_prefix_share: float = DEFAULT_CACHE_HEALTH_PREFIX_SHARE
    #: The smallest context the OS will compact past an expired cache, or None to never
    #: compact. Fleet-wide for `DEFAULT_CACHE_HEALTH_WINDOW_DAYS`' reason and one more:
    #: the break-even is a property of the prompt cache's prices, which no project has
    #: its own copy of. See DEFAULT_COMPACT_MIN_CONTEXT.
    compact_min_context: int | None = DEFAULT_COMPACT_MIN_CONTEXT
    notification_sinks: list[str] = field(default_factory=lambda: ["log"])
    telegram_token_env: str = "JARVIS_TELEGRAM_TOKEN"
    telegram_chat_id_env: str = "JARVIS_TELEGRAM_CHAT_ID"
    ui_port: int = 8787
    # Where notification deep links point. Empty = http://127.0.0.1:<ui_port>;
    # set it when the UI is reachable under another host (tunnel, LAN, reverse proxy).
    ui_base_url: str = ""
    # Knowledge reaches workers as an index they query on demand, so prompt cost stays
    # flat as the base grows. Only entries tagged `pinned` are pasted in full.
    knowledge_inject_limit: int = 8      # max pinned entries injected verbatim
    knowledge_digest_limit: int = 40     # max index lines
    knowledge_digest_chars: int = 4000   # hard char budget for those lines
    neo: NeoConfig = field(default_factory=NeoConfig)
    validation: ValidationConfig = field(default_factory=ValidationConfig)
    inspect: InspectConfig = field(default_factory=InspectConfig)
    concision: ConcisionConfig = field(default_factory=ConcisionConfig)
    supervisor: SupervisorConfig = field(default_factory=SupervisorConfig)
    messaging: MessagingConfig = field(default_factory=MessagingConfig)
    bugs: BugsConfig = field(default_factory=BugsConfig)
    schedule: ScheduleConfig = field(default_factory=ScheduleConfig)
    wiring: WiringConfig = field(default_factory=WiringConfig)


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


def _err(msg: str) -> CatalogError:
    return CatalogError(f"catalog error: {msg}")


def _positive_int_or_err(os_raw: dict[str, Any], key: str, default: int) -> int:
    value = os_raw.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _err(f"os.{key} must be an integer")
    return value


def _cold_prefix_floor_or_err(os_raw: dict[str, Any]) -> tuple[int, int]:
    """`os.cold_prefix_floor` and its guard rail, validated at boot not at report time.

    Rejected here for the same reason the autocompact window is: a bad value would
    otherwise surface far from its cause — as a cost report quietly reclassifying every
    boundary, which reads as a finding rather than as a config error.
    """
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
    """`os.compact_min_context`, validated at boot. An explicit null means "never".

    Absent and null differ here exactly as they do for the autocompact window, and for
    the same reason: silence inherits the measured default, and switching a cost control
    off has to be said out loud. Null is the ONLY off switch — there is deliberately no
    `enabled` flag and no per-order opt-in, because an automatic remedy nobody has to
    remember is the whole point (the pinned self-healing learning).

    The floor under the floor is `COMPACT_MIN_CONTEXT_MIN`: below it the compaction
    costs more than the re-write it replaces on every boundary the fleet has ever
    recorded, so a value there is a typo rather than a policy.
    """
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


def _cache_health_or_err(os_raw: dict[str, Any]) -> tuple[int, int, int, float]:
    """The cache-health window and its three floors, validated at boot.

    Rejected here rather than at report time for `_cold_prefix_floor_or_err`'s reason:
    a bad value would surface as a post-condition that never fires, or one that fires on
    three orders — a config error wearing a finding's clothes.
    """
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


def load_catalog(path: str | Path) -> Catalog:
    path = Path(path).expanduser()
    if not path.exists():
        raise _err(f"file not found: {path}")
    try:
        data = json.loads(path.read_text())
    except json.JSONDecodeError as e:
        raise _err(f"invalid JSON in {path}: {e}") from e
    return parse_catalog(data, source_path=path)


def _parse_panel(raw: Any) -> PanelConfig:
    """`os.neo.panel`, validated against the vocabularies it names.

    A roster naming a seat that does not exist, or a kind that is not a question kind, is
    a CatalogError rather than a silently dropped entry — the same rule an invalid
    `permission_mode` follows, and for a sharper reason here: every seat past `premise` is
    a safety check, so a typo that quietly drops one removes a check and tells nobody.

    `neo_store.SEATS` is the vocabulary, NOT the set of seats shipped in this build. A
    roster may name a seat whose definition arrives in a later release: that is a config
    written ahead of the code, and it is caught loudly at run time (the seat records a
    `failed` opinion and the panel proceeds) rather than refusing to boot the whole fleet
    over a name the OS does recognise.
    """
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
    """`os.validation` — or a project's override of it — against the seat vocabulary.

    `base` is what an omitted key falls through to, and it is the whole mechanism behind
    per-project validation: `os.validation` parses against the shipped defaults, then
    each project parses against the OS answer, so a project naming one key inherits the
    other seven (design doc §1.2). `where` only labels the error messages.

    Modelled on `_parse_panel`, and it makes the same distinction for the same reason:
    `project_store.VALIDATOR_SEATS` is the VOCABULARY, not the set of seats whose
    markdown ships in this build. A roster may name a seat whose definition arrives in a
    later release — config written ahead of the code — and that must parse; the missing
    definition is caught loudly at run time (the seat records a `failed` opinion and the
    panel proceeds) rather than refusing to boot the whole fleet. A name that is not in
    the vocabulary at all is a different thing: a typo that would silently remove a
    reviewer, so it is a CatalogError naming it.
    """
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
    diff_chars = int(raw.get("diff_chars", base.diff_chars))
    if diff_chars < 1:
        raise _err(f"{where}.diff_chars must be >= 1")
    max_follow_ups = int(raw.get("max_follow_ups", base.max_follow_ups))
    if max_follow_ups < 0:
        # 0 is legal and is NOT the same setting as `follow_ups: false`: it files
        # nothing while leaving every dropped finding counted on the round's event.
        raise _err(f"{where}.max_follow_ups must be >= 0")
    return ValidationConfig(
        enabled=bool(raw.get("enabled", base.enabled)),
        roster=roster,
        seat_models={k: str(v) for k, v in seat_models.items()},
        chair_model=str(raw.get("chair_model", base.chair_model) or ""),
        timeout=timeout,
        max_rounds=max_rounds,
        diff_chars=diff_chars,
        feature_units=bool(raw.get("feature_units", base.feature_units)),
        # Same field-level fallback as every flag in this block — see `auto_merge` below.
        follow_ups=bool(raw.get("follow_ups", base.follow_ups)),
        max_follow_ups=max_follow_ups,
        # `base.auto_merge` is the fleet answer when this project names nothing, and the
        # shipped `False` when the fleet names nothing either — the field-level fallback
        # that makes this per-project rather than global. A project opts in by naming it.
        auto_merge=bool(raw.get("auto_merge", base.auto_merge)),
        # Same fallback, same reason — see `ValidationConfig.auto_review`.
        auto_review=bool(raw.get("auto_review", base.auto_review)),
    )


def _parse_inspect(raw: Any, base: InspectConfig | None = None,
                   where: str = "os.inspect") -> InspectConfig:
    """`os.inspect`, or a project's override of it, with absurd values refused.

    `base` is what an omitted key falls through to — the same field-level inheritance
    `_parse_validation` uses (kn-6ca2bcd9): `os.inspect` parses against the shipped
    defaults and each project parses against the OS answer, so a project naming one key
    inherits the rest and no caller ever has to consult two objects.

    Every COUNT is REFUSED rather than clamped below 1. Zero would report every write a
    session makes and flag every work order the fleet runs — the exact failure the
    defaults were measured to avoid — and it arrives by a typo in a `jarvis config set`,
    so it is caught where the message can name the key.

    THE THREE VOCABULARIES ARE KEPT APART because one rule cannot serve them: a SHARE is
    refused outside `(0, 1]` — above 1 it is not a stricter alarm, it is one that can
    never fire — and the one MONEY floor is refused below 0, since zero is a project
    asking to hear about its tax however little it spent. Running either through the
    ">= 1" rule would reject every legal value.
    """
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
        alarm_join_seconds=int(raw.get("alarm_join_seconds",
                                       base.alarm_join_seconds)),
        alarm_write_tokens=int(raw.get("alarm_write_tokens",
                                       base.alarm_write_tokens)),
        alarm_parked_minutes=int(raw.get("alarm_parked_minutes",
                                         base.alarm_parked_minutes)),
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
        alarm_cache_1h_tokens=int(raw.get("alarm_cache_1h_tokens",
                                          base.alarm_cache_1h_tokens)),
        alarm_cache_1h_dispatched_tokens=int(
            raw.get("alarm_cache_1h_dispatched_tokens",
                    base.alarm_cache_1h_dispatched_tokens)),
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


def _parse_concision(raw: Any, base: ConcisionConfig | None = None,
                     where: str = "os.concision") -> ConcisionConfig:
    """`os.concision`, or a project's override of it — field-level, like `_parse_inspect`.

    Negative is refused; zero is not (see `ConcisionConfig`). A cap below the refusal
    message's own length would deny every finish and leave the worker no shape of
    summary that passes, so the floor is the smallest cap a real headline fits in.
    """
    base = base or ConcisionConfig()
    if not isinstance(raw, dict):
        raise _err(f'"{where}" must be an object')
    words = int(raw.get("summary_max_words", base.summary_max_words))
    if words < 0:
        raise _err(f"{where}.summary_max_words must be 0 (off) or more, got {words}")
    if 0 < words < 20:
        raise _err(f"{where}.summary_max_words of {words} leaves no summary that can "
                   f"pass; use 0 to switch the cap off")
    return ConcisionConfig(summary_max_words=words)


def _parse_bugs(raw: Any, base: BugsConfig | None = None,
                where: str = "os.bugs") -> BugsConfig:
    """`os.bugs`, or a project's override of it — field-level, like `_parse_inspect`.

    An empty label is refused rather than silently disabling the in-progress signal:
    `jarvis config set <p> bugs.label ""` is a plausible typo, and a tracker that
    quietly stopped saying which issues are being worked is the failure issue #240 is
    about.
    """
    base = base or BugsConfig()
    if not isinstance(raw, dict):
        raise _err(f'"{where}" must be an object')
    label = str(raw.get("label", base.label) or "").strip()
    if not label:
        raise _err(f"{where}.label must not be empty")
    # SHAPE, not just non-emptiness (review round 1). This string becomes a `gh` argument
    # (`issues.checked_label` is the layer that cannot be skipped); catching it here is
    # what lets the message name the key the typo is in.
    from .issues import LABEL_RE
    if not LABEL_RE.match(label):
        raise _err(f"{where}.label must start with a letter or digit and use only "
                   f"letters, digits, spaces and ._:/- (got {label!r})")
    return BugsConfig(label=label)


def _parse_messaging(raw: Any, base: MessagingConfig | None = None,
                     where: str = "os.messaging") -> MessagingConfig:
    """`os.messaging`, or a project's override of it — field-level, like `_parse_inspect`.

    Refused rather than clamped below 1, for that function's reason: zero would call
    every message the fleet has just queued stuck, and it arrives by a typo in a
    `jarvis config set` that this is the last place able to name.
    """
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


def _parse_wiring(raw: Any, base: WiringConfig | None = None,
                  where: str = "os.wiring") -> WiringConfig:
    """`os.wiring`, or a project's override of it — field-level, like `_parse_inspect`.

    The ids are NOT validated against what the machine currently has, and that is the
    one decision here worth stating. A catalog is read by the daemon on a box where the
    user's Claude configuration can change under it — uninstall a plugin and a list that
    named it would fail the whole catalog, taking the fleet down over a deselection that
    has simply come true. An id naming nothing is inert, exactly as an allow rule naming
    an absent tool is (`dispatch.SERENA_TOOL_PREFIXES`); `jarvis config wiring` is where
    a stale entry is visible, and it says so there.
    """
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
    )


def _parse_schedule(raw: Any, base: ScheduleConfig | None = None,
                   where: str = "os.schedule") -> ScheduleConfig:
    """`os.schedule`, or a project's override of it — field-level, like `_parse_inspect`.

    `jobs` is validated against `schedule.JOBS` rather than accepted as written: an id
    that names no job would otherwise be a job that silently never runs, and the whole
    point of this block is that the user can tell what the OS is about to do on its own.
    """
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


#: Fields of `SupervisorConfig` that are NOT whole numbers. The reflective parse below
#: casts everything else with `int()`, so a non-numeric field missing from this set is a
#: `TypeError` on every catalog load — or, for a bool, a silent `int(False) == 0` that
#: trips the `>= 1` floor instead and blames the wrong key.
_SUPERVISOR_NON_NUMERIC = ("enabled", "model", "probes", "health_enabled", "remedies")


def _parse_remedies(raw: Any, base: RemedyConfig, where: str) -> RemedyConfig:
    """`supervisor.remedies`, or a project's override of it — field-level, like the rest.

    An unknown id is refused with the known ones named. Same call `GateConfig.parse`
    makes about an unknown gate name and for the same reason: a permission the user
    believes they granted, silently unset, is the failure this whole block exists to
    make impossible.
    """
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


def _parse_probes(raw: Any, base: tuple[probes_mod.HealthProbe, ...],
                  where: str) -> tuple[probes_mod.HealthProbe, ...]:
    """`supervisor.probes` merged over `base` by id — `probes.resolve`'s rule (§2).

    Shape is refused HERE, where the message can name the offending id, and the merge
    happens in `probes` because the merge rule is that module's decision. An entry names
    only the fields it changes, so an unknown key is IGNORED rather than refused — the
    same forward compatibility `parse_catalog` gives every other block.
    """
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
    """The refusals that need the RESOLVED list and the numbers beside it, so they run
    after both exist."""
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
    """`os.supervisor`, or a project's override of it — §2, and `_parse_inspect`'s shape.

    Every number is refused below 1, and `timeout` at or above `stale_reviewing_seconds`:
    below that cutoff a claim is reclaimed out from under a call that is still running
    and one alarm is judged twice. The numeric fields are read reflectively so a field
    added above reaches the catalog with no edit here — which is why the exclusion set is
    a named constant and every non-number must join it. Excluding a field is only half
    the fix: it must then be read explicitly below, or the catalog can never set it.
    """
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
        panel=_parse_panel(neo_raw.get("panel", {})),
    )

    cold_floor, cold_floor_max = _cold_prefix_floor_or_err(os_raw)
    health_days, health_orders, health_bounds, health_prefix = _cache_health_or_err(os_raw)
    os_cfg = OsConfig(
        default_model=defaults.get("model", DEFAULT_MODEL),
        default_effort=defaults.get("effort"),
        default_permission_mode=defaults.get("permission_mode", DEFAULT_PERMISSION_MODE),
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
        knowledge_inject_limit=int(os_raw.get("knowledge_inject_limit", 8)),
        knowledge_digest_limit=int(os_raw.get("knowledge_digest_limit", 40)),
        knowledge_digest_chars=int(os_raw.get("knowledge_digest_chars", 4000)),
        neo=neo_cfg,
        validation=_parse_validation(os_raw.get("validation", {})),
        inspect=_parse_inspect(os_raw.get("inspect", {})),
        concision=_parse_concision(os_raw.get("concision", {})),
        supervisor=_parse_supervisor(os_raw.get("supervisor", {})),
        messaging=_parse_messaging(os_raw.get("messaging", {})),
        bugs=_parse_bugs(os_raw.get("bugs", {})),
        schedule=_parse_schedule(os_raw.get("schedule", {})),
        wiring=_parse_wiring(os_raw.get("wiring", {})),
    )
    if os_cfg.default_permission_mode not in VALID_PERMISSION_MODES:
        raise _err(f"os.defaults.permission_mode {os_cfg.default_permission_mode!r} not in {sorted(VALID_PERMISSION_MODES)}")
    if os_cfg.default_max_concurrent < 1:
        raise _err("os.defaults.max_concurrent must be >= 1")
    if os_cfg.max_in_flight < 1:
        raise _err("os.defaults.max_in_flight must be >= 1")

    projects_raw = data.get("projects", [])
    if not isinstance(projects_raw, list):
        raise _err('"projects" must be a list')
    # An empty fleet is valid: a standby instance (e.g. a fresh production
    # deployment) boots with no projects and has them onboarded later.

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
        max_conc = int(p.get("max_concurrent", os_cfg.default_max_concurrent))
        if max_conc < 1:
            raise _err(f"project {name}: max_concurrent must be >= 1")
        worker = WorkerDefaults(
            model=w.get("model") or p.get("model") or os_cfg.default_model,
            effort=w.get("effort", os_cfg.default_effort),
            permission_mode=pmode,
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
        concision_cfg = _parse_concision(
            p.get("concision", {}), base=os_cfg.concision,
            where=f"projects[{i}] ({name}).concision")
        supervisor_cfg = _parse_supervisor(
            p.get("supervisor", {}), base=os_cfg.supervisor,
            where=f"projects[{i}] ({name}).supervisor")
        messaging_cfg = _parse_messaging(
            p.get("messaging", {}), base=os_cfg.messaging,
            where=f"projects[{i}] ({name}).messaging")
        bugs_cfg = _parse_bugs(
            p.get("bugs", {}), base=os_cfg.bugs,
            where=f"projects[{i}] ({name}).bugs")
        schedule_cfg = _parse_schedule(
            p.get("schedule", {}), base=os_cfg.schedule,
            where=f"projects[{i}] ({name}).schedule")
        wiring_cfg = _parse_wiring(
            p.get("wiring", {}), base=os_cfg.wiring,
            where=f"projects[{i}] ({name}).wiring")
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
                concision=concision_cfg,
                supervisor=supervisor_cfg,
                messaging=messaging_cfg,
                bugs=bugs_cfg,
                schedule=schedule_cfg,
                wiring=wiring_cfg,
                raw=p,
            )
        )

    return Catalog(os=os_cfg, projects=projects, source_path=source_path)


def validate_paths(catalog: Catalog) -> list[str]:
    """Return human-readable problems with project paths (missing dir, not a git repo)."""
    problems = []
    for p in catalog.projects:
        if not p.path.is_dir():
            problems.append(f"{p.name}: path does not exist: {p.path}")
        elif not (p.path / ".git").exists():
            problems.append(f"{p.name}: not a git repository ({p.path}) — run `git init` first")
    return problems
