"""Catalog: the JSON file describing the fleet of projects Jarvis manages."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

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
# project, or fleet-wide via os.defaults.max_concurrent).
DEFAULT_MAX_CONCURRENT = 5


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
AUTOCOMPACT_MIN = 100_000    # `claude --autocompact` rejects anything under this
AUTOCOMPACT_MAX = 1_000_000  # ... or over this


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

# Truncation limit for the diff a seat is shown.
DEFAULT_VALIDATION_DIFF_CHARS = 60000


@dataclass
class ValidationConfig:
    """An independent panel judging a work order's or feature order's claim of done.

    SHIPS DISABLED, and that is a requirement rather than caution: at this default the
    OS must behave exactly as it does today — same statuses reached, same events, same
    number of `claude` calls, and not one row in `validation_rounds`. A round is roughly
    five headless calls over a diff of up to `diff_chars`, up to `max_rounds` times, on
    every unit in the fleet, so enabling it is a catalog edit gated on a measurement.

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

#: A turn still running after this long is burning money now. p95 of a turn's ACTIVE time
#: is 59 minutes, so this fires on 16% of work orders — and it sits well below
#: `worker_session.TURN_STALL_SECONDS` (6h), which reports a different fact: hung, not
#: expensive.
DEFAULT_INSPECT_ALARM_TURN_MINUTES = 60

#: A blocking join still open after this long. THE ONLY THRESHOLD HERE THAT IS PRINCIPLED
#: RATHER THAN EMPIRICAL: it is the 5-minute cache TTL itself, past which the prefix is
#: certainly cold and the wait will be paid for a second time as a re-write. Fires on 2%.
DEFAULT_INSPECT_ALARM_JOIN_SECONDS = 300

#: One call re-sending this much of the conversation. p95 of the largest re-write per work
#: order (the median is 130,519), so it fires on 5% — about $1.88 at Opus list prices in a
#: single event.
DEFAULT_INSPECT_ALARM_WRITE_TOKENS = 300_000


@dataclass
class InspectConfig:
    """What `jarvis inspect` reports, and when the OS raises a turn that is still running.

    Per project as well as fleet-wide, with field-level inheritance (`_parse_inspect`):
    a project that names one key keeps the OS answer for the rest. That matters because
    the alarm thresholds are a statement about what is NORMAL, and normal differs by
    project — an hour-long turn is routine where the work is a design document and a
    symptom where it is a one-file fix.

    `enabled` turns only the ALARM off, never the report: `jarvis inspect` reads files
    that are already on disk and costs nothing until someone runs it, whereas the alarm
    reads a transcript per running work order per reconcile tick.
    """

    enabled: bool = True
    report_write_floor: int = DEFAULT_INSPECT_REPORT_WRITE_FLOOR
    report_join_floor: int = DEFAULT_INSPECT_REPORT_JOIN_FLOOR
    quote_chars: int = DEFAULT_INSPECT_QUOTE_CHARS
    alarm_turn_minutes: int = DEFAULT_INSPECT_ALARM_TURN_MINUTES
    alarm_join_seconds: int = DEFAULT_INSPECT_ALARM_JOIN_SECONDS
    alarm_write_tokens: int = DEFAULT_INSPECT_ALARM_WRITE_TOKENS


# -- the supervisor: the agent that answers a cost alarm before the user has to
#
# ABOVE `ProjectSpec` BECAUSE `ProjectSpec` USES IT VIA `field(default_factory=…)`, which
# is evaluated at class-definition time — the trap that cost `ValidationConfig` a move
# (kn-6ca2bcd9). Nothing here belongs in `InspectConfig`: that block is thresholds, and
# `tests/test_inspection.py::test_nothing_in_the_module_hard_codes_a_threshold` AST-walks
# `inspection.py` for numeric literals against exactly that list.

#: An alarm older than this is never judged. Spend the user can no longer prevent is the
#: noise the whole mechanism was tuned to avoid, and a model call to describe it is money
#: spent on a turn that ended yesterday.
DEFAULT_SUPERVISOR_MAX_AGE_HOURS = 24


@dataclass
class SupervisorConfig:
    """The OS-level agent that reviews a cost alarm and either acks it or wants Neo.

    SHIPS DISABLED, and here that is stronger than the caution behind `PanelConfig`: the
    failure mode is the worst available. A wrong ack puts the attention flag down on a
    turn that is still burning, which is a strict regression on what PR 159 shipped —
    whereas every other disabled feature merely fails to add something. Turning it on is
    a catalog edit gated on the review loop being run by hand over real alarms; see §2 of
    docs/superpowers/specs/2026-08-31-the-supervisor.md.

    Field-level per-project inheritance (`_parse_supervisor`), the shape
    `_parse_validation` and `_parse_inspect` both use: what counts as an explicable turn
    differs by project for the same reason a threshold does, and `ProjectSpec.supervisor`
    is fully resolved so no caller consults two objects.
    """

    enabled: bool = False
    model: str = "opus"
    timeout: int = 300
    learnings_limit: int = 50
    max_age_hours: int = DEFAULT_SUPERVISOR_MAX_AGE_HOURS


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
    supervisor: SupervisorConfig = field(default_factory=SupervisorConfig)
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
    default_autocompact_window: int | None = DEFAULT_AUTOCOMPACT_WINDOW
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
    supervisor: SupervisorConfig = field(default_factory=SupervisorConfig)


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
    return ValidationConfig(
        enabled=bool(raw.get("enabled", base.enabled)),
        roster=roster,
        seat_models={k: str(v) for k, v in seat_models.items()},
        chair_model=str(raw.get("chair_model", base.chair_model) or ""),
        timeout=timeout,
        max_rounds=max_rounds,
        diff_chars=diff_chars,
        feature_units=bool(raw.get("feature_units", base.feature_units)),
    )


def _parse_inspect(raw: Any, base: InspectConfig | None = None,
                   where: str = "os.inspect") -> InspectConfig:
    """`os.inspect`, or a project's override of it, with absurd values refused.

    `base` is what an omitted key falls through to — the same field-level inheritance
    `_parse_validation` uses (kn-6ca2bcd9): `os.inspect` parses against the shipped
    defaults and each project parses against the OS answer, so a project naming one key
    inherits the other five and no caller ever has to consult two objects.

    Every threshold is REFUSED rather than clamped below 1. Zero would report every write
    a session makes and flag every work order the fleet runs — the exact failure the
    defaults were measured to avoid — and it arrives by a typo in a `jarvis config set`,
    so it is caught where the message can name the key.
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
        alarm_join_seconds=int(raw.get("alarm_join_seconds",
                                       base.alarm_join_seconds)),
        alarm_write_tokens=int(raw.get("alarm_write_tokens",
                                       base.alarm_write_tokens)),
    )
    for name, value in vars(cfg).items():
        if name != "enabled" and value < 1:
            raise _err(f"{where}.{name} must be >= 1")
    return cfg


def _parse_supervisor(raw: Any, base: SupervisorConfig | None = None,
                      where: str = "os.supervisor") -> SupervisorConfig:
    """`os.supervisor`, or a project's override of it, with the same field-level
    inheritance `_parse_inspect` uses (kn-6ca2bcd9).

    `timeout` is refused below 1 for the reason a threshold is, and refused at or above
    `supervisor.STALE_REVIEWING_SECONDS` for a sharper one: a claim reclaimed out from
    under a call that is still running gets the same alarm judged twice, and the second
    verdict overwrites the first. That relation is the whole point of the stale cutoff,
    so it is enforced where a `jarvis config set` typo can be named rather than left to
    a comment.
    """
    from .supervisor import STALE_REVIEWING_SECONDS

    base = base or SupervisorConfig()
    if not isinstance(raw, dict):
        raise _err(f'"{where}" must be an object')
    cfg = SupervisorConfig(
        enabled=bool(raw.get("enabled", base.enabled)),
        model=str(raw.get("model", base.model) or base.model),
        timeout=int(raw.get("timeout", base.timeout)),
        learnings_limit=int(raw.get("learnings_limit", base.learnings_limit)),
        max_age_hours=int(raw.get("max_age_hours", base.max_age_hours)),
    )
    for name, value in vars(cfg).items():
        if isinstance(value, int) and not isinstance(value, bool) and value < 1:
            raise _err(f"{where}.{name} must be >= 1")
    if cfg.timeout >= STALE_REVIEWING_SECONDS:
        raise _err(f"{where}.timeout must be under {STALE_REVIEWING_SECONDS}s "
                   f"(supervisor.STALE_REVIEWING_SECONDS), or a claim is reclaimed out "
                   f"from under a call that is still running and one alarm is judged "
                   f"twice")
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

    os_cfg = OsConfig(
        default_model=defaults.get("model", DEFAULT_MODEL),
        default_effort=defaults.get("effort"),
        default_permission_mode=defaults.get("permission_mode", DEFAULT_PERMISSION_MODE),
        default_max_concurrent=int(defaults.get("max_concurrent", DEFAULT_MAX_CONCURRENT)),
        default_autocompact_window=_autocompact_or_err(
            defaults, "autocompact_window", "os.defaults.autocompact_window",
            DEFAULT_AUTOCOMPACT_WINDOW),
        notification_sinks=notif.get("sinks", ["log"]),
        telegram_token_env=telegram.get("token_env", "JARVIS_TELEGRAM_TOKEN"),
        telegram_chat_id_env=telegram.get("chat_id_env", "JARVIS_TELEGRAM_CHAT_ID"),
        ui_port=ui.get("port", 8787),
        ui_base_url=str(ui.get("base_url", "") or "").rstrip("/"),
        knowledge_inject_limit=int(os_raw.get("knowledge_inject_limit", 8)),
        knowledge_digest_limit=int(os_raw.get("knowledge_digest_limit", 40)),
        knowledge_digest_chars=int(os_raw.get("knowledge_digest_chars", 4000)),
        neo=neo_cfg,
        validation=_parse_validation(os_raw.get("validation", {})),
        inspect=_parse_inspect(os_raw.get("inspect", {})),
        supervisor=_parse_supervisor(os_raw.get("supervisor", {})),
    )
    if os_cfg.default_permission_mode not in VALID_PERMISSION_MODES:
        raise _err(f"os.defaults.permission_mode {os_cfg.default_permission_mode!r} not in {sorted(VALID_PERMISSION_MODES)}")
    if os_cfg.default_max_concurrent < 1:
        raise _err("os.defaults.max_concurrent must be >= 1")

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
        supervisor_cfg = _parse_supervisor(
            p.get("supervisor", {}), base=os_cfg.supervisor,
            where=f"projects[{i}] ({name}).supervisor")
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
                supervisor=supervisor_cfg,
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
