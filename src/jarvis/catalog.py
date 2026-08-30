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
COLD_PREFIX_FLOOR_MAX = 100_000  # above this it would swallow real boundaries


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
    #: Read by the cost surfaces, not by a worker launch — see DEFAULT_COLD_PREFIX_FLOOR.
    cold_prefix_floor: int = DEFAULT_COLD_PREFIX_FLOOR
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


def _cold_prefix_floor_or_err(os_raw: dict[str, Any]) -> int:
    """`os.cold_prefix_floor`, validated at boot rather than at report time.

    Rejected here for the same reason the autocompact window is: a bad value would
    otherwise surface far from its cause — as a cost report quietly reclassifying every
    boundary, which reads as a finding rather than as a config error.
    """
    value = os_raw.get("cold_prefix_floor", DEFAULT_COLD_PREFIX_FLOOR)
    if isinstance(value, bool) or not isinstance(value, int):
        raise _err("os.cold_prefix_floor must be an integer")
    if not 0 <= value <= COLD_PREFIX_FLOOR_MAX:
        raise _err(f"os.cold_prefix_floor {value} out of range "
                   f"0-{COLD_PREFIX_FLOOR_MAX}")
    return value


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
        cold_prefix_floor=_cold_prefix_floor_or_err(os_raw),
        knowledge_inject_limit=int(os_raw.get("knowledge_inject_limit", 8)),
        knowledge_digest_limit=int(os_raw.get("knowledge_digest_limit", 40)),
        knowledge_digest_chars=int(os_raw.get("knowledge_digest_chars", 4000)),
        neo=neo_cfg,
        validation=_parse_validation(os_raw.get("validation", {})),
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
