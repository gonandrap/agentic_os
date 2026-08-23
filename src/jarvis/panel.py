"""Neo as a panel — a roster of profiled seats, one blind round, one verdict out.

Today Neo is ONE headless call: `neo.answer_question` builds one system prompt and asks
one model for one strict-JSON verdict. A single prompt has exactly one FRONT, and the
only failure the record ever showed was a concern that was not first getting outcompeted
by one that was (`gates.REVIEWER_PERSONA`'s premise check had to be moved to the top
before Neo stopped denying commands that performed no privileged action at all). Four
concerns cannot each be first in one prompt. See
docs/superpowers/specs/2026-08-02-neo-team-design.md.

This module is that primitive, and all five seats now ship: `premise` (was this even the
question that was asked), `record` (does this contradict a standing ruling), `blast` (what
does being wrong cost, and it holds the only veto), `taste` (what the user meant, and no
veto at all), and `chair`, which turns the seats' opinions into the one verdict
`neo.parse_verdict` already accepts.

FOUR THINGS THAT LOOK LIKE DETAILS AND ARE NOT:

**The panel ships DISABLED, and at that default the OS must behave byte-identically to
today** — same number of Claude calls, same system prompt bytes, same message to the
worker. Nothing in this module runs unless a catalog turns it on.

**`neo` MUST NEVER IMPORT THIS MODULE.** The design says "`neo.answer_question` becomes a
caller of it" and "if the Premise Sceptic fails, fall back to today's single-agent path",
which taken literally is an import cycle in a codebase that has none. The seam is instead
a `neo.drain_queue(answer=…)` keyword the daemon injects: this module imports `neo`, `neo`
knows nothing about panels, and the fallback is simply not passing `answer=`.

**ON `route=fast` THE PREMISE SEAT'S OWN REPLY IS THE VERDICT AND THE CHAIR IS SKIPPED,
AND THE PREMISE SEAT THEREFORE RUNS FIRST AND ALONE.** The design's fast path claimed "one
call total" for the ~95% of gate reviews that are classifier false positives, but a premise
call plus a chair call is TWO — a 2x cost and latency regression on Neo's highest-volume
channel. So the premise seat emits the full verdict shape plus a `route`, and on the fast
route that reply is what the worker reads. That arithmetic only survives the full roster if
the seat that ROUTES runs before the seats it might route past: a single blind round over
all four would make a fast dismissal cost four calls instead of one. Two safety rules live
in CODE rather than in a prompt, because a prompt is a preference a model can talk itself
out of: see `fast_is_permitted`.

**THE VETO TABLE IS A PURE FUNCTION, NOT A CLAUSE IN THE CHAIR'S PROMPT.** See `arbitrate`.
The one Neo failure the record actually contains was a persona whose clause ordering
structurally forced the wrong answer — so a safety rule expressed as prompt text is a rule
subject to prompt luck, which is the exact failure this whole feature exists to fix.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from . import claude_cli, seats, structured
from .bootstrap import ASSETS
from .neo_store import SEATS, NeoStore
from .seats import Opinion, SeatError, parse_definition

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .catalog import NeoConfig

log = logging.getLogger("neo.panel")

#: Where the seat definitions live. DELIBERATELY NOT `assets/agents/`: `bootstrap._rebuild`
#: copytrees that directory wholesale into every feature-order planner's `.claude/agents/`,
#: so a Neo seat dropped there — subdirectory or not — becomes a bogus subagent every
#: planner session can invoke.
SEAT_ASSETS = ASSETS / "neo-seats"

#: First line of every seat's system prompt. Machine-readable on purpose: it identifies
#: the seat to anything reading the call (the test fake keys on it) without depending on
#: the mandate's prose, and it is constant per seat, so it does not disturb the per-seat
#: cached prompt prefix.
SEAT_HEADER = "# Neo panel seat: {seat}"

#: Routes. `fast` = the premise seat answered and the chair was skipped; `panel` = the
#: seats deliberated and the chair synthesised; `single` = the panel stepped aside and
#: today's single-agent path answered (see `decide`).
ROUTES = ("fast", "panel", "single")


#: Neo's roster, as `seats.py` sees it: where the mandates live, what names are legal and
#: the header that identifies a call as one of THESE seats. Built per call rather than held
#: as a module constant, because `SEAT_ASSETS` is a module global a test swaps to unship a
#: seat — a constant captured at import time would ignore that, and the "no markdown ships
#: for this seat" path would stop being reachable.
def neo_roster() -> seats.Roster:
    return seats.Roster(assets=SEAT_ASSETS, vocabulary=SEATS, header=SEAT_HEADER)


def seat_path(seat: str) -> Path:
    return neo_roster().path(seat)


def shipped_seats() -> tuple[str, ...]:
    """The seats whose definition ships in this build, in `SEATS` order."""
    return seats.shipped(neo_roster())


def definition(seat: str) -> tuple[dict[str, str], str]:
    """The shipped (frontmatter, mandate) for one Neo seat.

    A thin binding of `seats.definition` to this roster. The cache lives there and is keyed
    on the ROSTER as well as the seat: `chair.md` ships in `neo-seats/` and in
    `validator-seats/`, and a name-only key would let whichever was read first answer for
    both. `cache_clear` is re-exported because clearing it is how a test swaps the seat
    directory under `SEAT_ASSETS`.
    """
    return seats.definition(neo_roster(), seat)


definition.cache_clear = seats.definition.cache_clear  # type: ignore[attr-defined]


def seat_model(seat: str, cfg: NeoConfig) -> str:
    """Which model this seat runs on.

    Most specific wins: the catalog's per-seat map, then `chair_model` for the chair,
    then the definition's own `model:` key, then Neo's model. The chair should keep the
    expensive model when the seats do not — it is the one writing what a human reads.
    """
    panel = cfg.panel
    explicit = panel.seat_models.get(seat) or (panel.chair_model if seat == "chair" else "")
    try:
        declared = definition(seat)[0].get("model", "")
    except SeatError:
        declared = ""
    return explicit or declared or cfg.model


def build_seat_system_prompt(store: NeoStore, project: str, seat: str,
                             learnings_limit: int = 50) -> str:
    """One seat's mandate + the learnings scoped to it. Byte-stable per seat.

    Mirrors `neo.build_system_prompt`, and for the same reason: the prefix has to be
    identical call to call or every seat pays a full prompt-cache miss. Running seats in
    parallel does not disturb that — the prefix is per SEAT, not per queue position.

    `store.learnings(project, seat=seat)` returns the global learnings PLUS this seat's.
    The default (no seat) returns only the global ones, which is what keeps a seat-scoped
    correction out of the single-agent path's cached prefix.

    The character budget comes from `neo.render_learnings` rather than being reimplemented
    here: a panel round pays for the block once PER SEAT, so the one prompt component that
    grows without limit is worth five times as much here as it is on the single-agent path.
    """
    from . import neo

    _, mandate = definition(seat)
    parts = [SEAT_HEADER.format(seat=seat), "", mandate, "",
             "# Learnings (from the user's reviews of your past answers)"]
    parts += neo.render_learnings(
        store.learnings(project, limit=learnings_limit, seat=seat))
    return "\n".join(parts)


# -- one seat's contribution ------------------------------------------------------------


#: One seat's contribution, and the runner that produces it, both live in `seats.py` —
#: this panel and the validation panel are two rosters of the same machine. `replied` is
#: carried across with them: `decide`'s fallback to the single agent turns on the
#: difference between a premise seat that SAID nothing and one that said something
#: unusable, and that distinction is the first thing a copy would lose.
_run_seat = seats._run_seat


def _round(store: NeoStore, q: dict[str, Any], cfg: NeoConfig,
           names: list[str], record: Any = None) -> list[Opinion]:
    """Run every seat blind, concurrently, and record what each said.

    Concurrent INSIDE the daemon's single Neo thread: the queue still drains FIFO, one
    question at a time, so the prompt-cache economics of the drain are untouched. The
    per-seat timeout is `PanelConfig.timeout` and is bounded well below Neo's 300s call
    timeout on purpose — the whole FIFO drain, and every worker parked behind it, waits
    on the slowest seat.
    """
    from .paths import ensure_home

    # Every prompt is built HERE, on the calling thread, before anything fans out: the
    # store's connection is thread-local, and building them up front also makes what each
    # seat was asked deterministic.
    home = ensure_home()
    prompt = _question_prompt(q)
    systems: dict[str, str] = {}
    missing: list[Opinion] = []
    for seat in names:
        try:
            systems[seat] = build_seat_system_prompt(store, q["project"], seat,
                                                     cfg.learnings_limit)
        except SeatError as e:
            # Not an outage: this build has no such seat. The panel proceeds without it
            # rather than stalling the drain, but it says so loudly and the row records
            # `failed` rather than `abstained` — a seat that CANNOT run is not one that
            # timed out.
            log.error("panel seat %s cannot run: %s", seat, e)
            missing.append(Opinion(seat=seat, raw=str(e), status="failed",
                                   replied=False))

    opinions = seats.run_blind(
        {seat: (system, prompt) for seat, system in systems.items()},
        models={seat: seat_model(seat, cfg) for seat in systems},
        timeout=cfg.panel.timeout, cwd=home)
    opinions += missing
    for op in opinions:
        _record(store, q, op, record)
    return opinions


def _record(store: NeoStore, q: dict[str, Any], op: Opinion,
            record: Any = None) -> None:
    """Persist one seat's contribution: what it said, and what it cost.

    ONE ROW PER SEAT in `agent_calls`, never one per question. Whether the panel earns
    its price is exactly the question of what the seats cost against what the single
    agent would have, and an aggregate cannot answer it — nor say which seat is the
    expensive one. A seat that never replied (`missing`, or a call that errored) has no
    usage to record, so it does not get a row: it cost nothing.
    """
    from . import agent_usage

    store.record_opinion(q["id"], op.seat, reply=op.raw, verdict=op.verdict,
                         route=op.route, status=op.status, model=op.model,
                         latency_ms=op.latency_ms)
    if op.usage is None:
        return
    (record or agent_usage.record)(
        "panel_seat", usage=op.usage, project=q.get("project") or "",
        wo_id=q.get("wo_id") or "", label=op.seat, model=op.model,
        question_id=q.get("id"), ok=op.status == "ok")


def _question_prompt(q: dict[str, Any]) -> str:
    from . import neo

    return neo.build_question_prompt(q)


# -- routing ------------------------------------------------------------------------------


def fast_is_permitted(kind: str, proposed: str) -> bool:
    """May this decision take the fast route, given what the premise seat proposed?

    THIS IS THE SAFETY RULE, AND IT IS IN CODE BECAUSE A PROMPT IS A PREFERENCE. The fast
    route skips the seat that owns the blast radius, so it is available only where being
    wrong is cheap:

    * a proposed `dismiss` — the command performed no privileged action, so there is
      nothing to authorise and nothing to get wrong except the classifier; or
    * an open question, which authorises nothing by construction.

    And a `approval` never reaches `approved` this way, however the premise seat routed
    it: a real privileged action always costs the full panel. That clause is redundant
    with the one below it today and is written out anyway, because it is the rule, and a
    rule that survives only as a consequence of another rule stops holding the moment
    that other rule is relaxed.
    """
    if kind == "approval" and proposed == "approved":
        return False
    return proposed == "dismissed" or kind == "question"


def _premise_route(op: Opinion, kind: str, cfg: NeoConfig) -> tuple[str, dict | None]:
    """(route, the premise seat's reply) — `panel` whenever anything is off.

    A reply that will not parse, that is not a verdict at all, or that carries no `route`
    key is treated as `panel`: fail toward the expensive-but-safe side, never toward the
    route that skips four seats.
    """
    data = op.data
    if not isinstance(data, dict) or "escalate" not in data:
        return "panel", None
    if not cfg.panel.fast_path or op.route != "fast":
        return "panel", data
    proposed = _proposed_verdict(op, data)
    if not fast_is_permitted(kind, proposed):
        log.info("panel: premise asked for the fast route on a %s proposing %r; "
                 "running the full panel", kind, proposed or "nothing")
        return "panel", data
    return "fast", data


def _proposed_verdict(op: Opinion, data: dict[str, Any]) -> str:
    """The premise seat's proposal, normalised to the store's vocabulary (`dismissed`,
    `approved`, `denied`) — or `""` when it proposed none.

    Normalised through `neo.parse_verdict` rather than by a second alias table here, so
    the panel and the single agent can never disagree about what "dismiss" means. The
    empty case is kept distinct: `parse_verdict` reads a missing verdict as `denied`,
    which is the right default for a gate but would read as a proposal the seat did not
    make.
    """
    from . import neo

    if not str(data.get("verdict") or "").strip():
        return ""
    return neo.parse_verdict(op.raw)["verdict"]


# -- arbitration: the veto table, as code ---------------------------------------------------

#: The seats the arbitration reads AT ALL, in the order their `reason` is preferred when
#: more than one of them forces the same escalation — the seat that owns `escalate` first,
#: then the seat that owns the record, then the seat that owns the premise.
#:
#: `taste` IS ABSENT DELIBERATELY, and its absence is the sharpest thing in this module. Its
#: failure mode is an annoying answer rather than a dangerous one, and a seat that could
#: block on taste would spend exactly the attention it exists to protect. A checker that
#: treated every objection alike — that read `escalate` without asking WHICH SEAT said it —
#: would pass every other test in the table and get this one row wrong, silently.
FORCES_ESCALATE = ("blast", "record", "premise")


def _reply(op: Mapping[str, Any]) -> dict[str, Any]:
    """One opinion's reply as an object, or `{}` when the seat said nothing usable.

    `{}` covers abstained, failed, an unrecognised status and output that will not parse —
    all of which are SILENCE. Silence is not a veto and it is not consent: it produces no
    signal here at all, and the decision goes on to the chair, whose mandate says in as
    many words never to treat silence as agreement.
    """
    if str(op.get("status") or "") != "ok":
        return {}
    data = structured.parse_json_object(str(op.get("reply") or ""))
    return data if isinstance(data, dict) else {}


def _raised(data: dict[str, Any], key: str) -> bool:
    """Did the seat raise this flag? Read PERMISSIVELY, on purpose.

    `bool()` rather than `is True`, so a model that wrote the string `"false"` forces an
    escalation it did not mean. That is deliberate and it is the only direction this
    function can be wrong in: every flag it reads points at `escalate`, so a permissive
    read costs one escalation too many and a strict read costs one too few.
    """
    return bool(data.get(key))


def _unresolvable(data: dict[str, Any]) -> bool:
    """Did the record seat report a contradiction it cannot square?

    EXACT match, not a substring one: `"resolvable"` is a suffix of `"unresolvable"`, and a
    contradiction the record itself settles must not force anything. The record seat has a
    second way to say the same thing (`escalate`), which is what covers a typo here.
    """
    return str(data.get("contradiction") or "").strip().lower() == "unresolvable"


def arbitrate(opinions: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """The veto table. Returns the outcome the seats FORCED, or None for "let the chair
    synthesise".

    Pure: plain dicts in, a verdict or None out. No store, no model, no clock. Each opinion
    is `{"seat", "status", "reply"}` — deliberately the shape of a `panel_opinions` row, so
    the same arbitration can be replayed over what was stored as well as over what was just
    collected.

    THE ROWS, and every one of them points the safe way:

    * `blast` returning `escalate` forces `escalate`.
    * `blast` raising `veto` forces `escalate` — that is the demotion of a proposed
      `dismiss` or `approve`, never a promotion of anything. Read unconditionally rather
      than only against a proposal on the table: the outcome is the same escalation on the
      row the design states, and the unconditional reading can only ever escalate MORE.
    * `record` reporting an unresolvable contradiction forces `escalate`, as does `record`
      returning `escalate`.
    * `premise` returning `escalate` forces `escalate` too. Its proposed `dismiss`, by
      contrast, is a PROPOSAL and nothing here reads it: the fast path is where a proposal
      becomes an answer, and only under `fast_is_permitted`.
    * `taste` forces nothing and vetoes nothing, however it replies.
    * NOTHING FORCES AN APPROVAL. Structurally, not by inspection: there is exactly one
      `return` that is not `None` and it is an escalation with `approve` False.

    Why this is not a paragraph in the chair's mandate: the one measured Neo failure on
    record was a persona whose clause ordering structurally forced the wrong answer. A
    safety rule that lives in prose is a rule that holds by prompt luck.
    """
    forcing: dict[str, str] = {}
    for op in opinions:
        seat = str(op.get("seat") or "")
        if seat not in FORCES_ESCALATE:
            continue
        data = _reply(op)
        if not data:
            continue
        forced = _raised(data, "escalate")
        if seat == "blast":
            forced = forced or _raised(data, "veto")
        elif seat == "record":
            forced = forced or _unresolvable(data)
        if forced:
            forcing[seat] = str(data.get("reason") or "").strip()

    if not forcing:
        return None

    # The forcing seat's own one line, verbatim and UNATTRIBUTED. This reason is delivered
    # to the user, and panel deliberation never leaves the room: quoting what a seat said
    # is the escalation's substance, naming which seat said it would be narrating the
    # panel. Seats whose reason came back empty are skipped rather than silencing the ones
    # that wrote something.
    reason = next((forcing[s] for s in FORCES_ESCALATE if forcing.get(s)), "")
    return {"escalate": True, "answer": "", "approve": False, "verdict": "denied",
            "dispatch": None,
            "reason": reason or "the panel could not settle this without you"}


# -- the entry point ------------------------------------------------------------------------


def decide(store: NeoStore, q: dict[str, Any], cfg: NeoConfig,
           record: Any = None) -> dict[str, Any]:
    """Answer one claimed question with the panel. Injected via `neo.drain_queue(answer=…)`.

    Returns EXACTLY what `neo.parse_verdict` returns — `escalate`, `answer`, `reason`,
    `verdict`, `approve`, `dispatch` — plus one additive `panel` key carrying the route
    and a per-seat summary. Additive is safe because every consumer (the daemon's
    `deliver`, the gate path, the plan path) reads by key and never by shape.

    The shape of a decision, in order: the `premise` seat alone, then either its own reply
    (the fast route) or the rest of the roster blind and in parallel, then `arbitrate`, and
    only if the seats forced nothing, the chair.

    Raises `claude_cli.ClaudeCliError` on total failure, so `drain_queue`'s existing
    rescue — mark the question failed, tell the worker — still applies unchanged.
    """
    from . import neo

    kind = q.get("kind") or "question"
    roster = [s for s in cfg.panel.roster if s != "chair"]

    # THE PREMISE SEAT RUNS FIRST AND ALONE. It is the seat that routes, and a route
    # decided after the seats it routes past have already been paid for is not a route. One
    # blind round over the whole roster would make a fast dismissal — the ~95% case on the
    # OS's highest-volume channel — cost four calls instead of one, which is the exact
    # regression the fast path exists to avoid.
    opinions = _round(store, q, cfg, [s for s in roster if s == "premise"], record)
    premise = next((op for op in opinions if op.seat == "premise"), None)

    if premise is None or not premise.replied:
        # The seat that routes never spoke, so there is nothing to route on. Fall back to
        # today's single-agent path: a Neo outage must never become a fleet stall, and the
        # single agent is a decision the OS already trusts.
        #
        # SILENCE, NOT UNUSABLE OUTPUT. A premise seat that replied with something that
        # will not parse HAS routed — toward `panel`, the expensive-but-safe side — and
        # `_premise_route` sends it there. Only a call that never produced a reply at all
        # falls back here.
        log.warning("panel: no usable premise opinion for question %s; "
                    "falling back to the single agent", q["id"])
        verdict = neo.answer_question(store, q, cfg.model, cfg.learnings_limit,
                                      record=record)
        return {**verdict, "panel": _summary("single", opinions)}

    route, _ = _premise_route(premise, kind, cfg)
    if route == "fast":
        # The premise seat's own reply IS the verdict; the chair is skipped, and so is
        # every other seat. This is what keeps the panel from being a 2x regression on the
        # OS's highest-volume channel.
        return {**neo.parse_verdict(premise.raw), "panel": _summary("fast", opinions)}

    # The full panel: the remaining seats run blind and in parallel. They do not see the
    # premise seat's reply — being second in wall-clock order is not being second in
    # knowledge, and agreement is only evidence if no seat could read another's answer.
    rest = [s for s in roster if s != "premise"]
    if rest:
        opinions = [*opinions, *_round(store, q, cfg, rest, record)]

    forced = arbitrate([{"seat": op.seat, "status": op.status, "reply": op.raw}
                        for op in opinions])
    if forced is not None:
        # The seats settled it and the chair does not get a vote on the safety rule. Its
        # prose would be discarded anyway — an escalation carries `answer: ""` — so
        # skipping it saves a call AND buys a rule no prompt can talk itself out of.
        # The route is still `panel`: the panel deliberated. That the chair did not run is
        # recorded by the absence of a `chair` row in `panel_opinions`.
        log.info("panel: the seats forced an escalation on question %s; "
                 "the chair was not run", q["id"])
        return {**forced, "panel": _summary("panel", opinions)}

    chair = _run_chair(store, q, cfg, opinions, record)
    opinions = [*opinions, chair]
    return {**neo.parse_verdict(chair.raw), "panel": _summary("panel", opinions)}


def _summary(route: str, opinions: list[Opinion]) -> dict[str, Any]:
    return {"route": route, "seats": [op.summary() for op in opinions]}


def _run_chair(store: NeoStore, q: dict[str, Any], cfg: NeoConfig,
               opinions: list[Opinion], record: Any = None) -> Opinion:
    """Synthesise. The chair is the one seat that is not blind — that is its whole job.

    A chair that cannot be reached is total failure, not a seat abstaining: there is no
    decision without it. The abstention is recorded first so the deliberation survives
    the exception, then `ClaudeCliError` propagates to `drain_queue`.
    """
    from .paths import ensure_home

    model = seat_model("chair", cfg)
    system = build_seat_system_prompt(store, q["project"], "chair", cfg.learnings_limit)
    prompt = build_chair_prompt(q, opinions)
    started = time.monotonic()
    try:
        result = claude_cli.run_headless_result(prompt, system_prompt=system, model=model,
                                                timeout=cfg.panel.timeout,
                                                cwd=ensure_home(), attribute=False)
    except claude_cli.ClaudeCliError as e:
        op = Opinion(seat="chair", raw=str(e), status="abstained", model=model,
                     latency_ms=int((time.monotonic() - started) * 1000))
        _record(store, q, op, record)
        raise
    data = structured.parse_json_object(result.text) or {}
    op = Opinion(seat="chair", raw=result.text, model=model, usage=result.usage,
                 latency_ms=int((time.monotonic() - started) * 1000),
                 verdict=str(data.get("verdict") or "").strip())
    _record(store, q, op, record)
    return op


def build_chair_prompt(q: dict[str, Any], opinions: list[Opinion]) -> str:
    """The question, then every seat's reply verbatim.

    Verbatim rather than summarised: a summariser between the seats and the chair is one
    more place for the compliance wording a learning mandates to be silently dropped.
    """
    parts = [_question_prompt(q), "", "# The panel's opinions",
             "Each seat answered blind — none of them saw another's reply, and none of "
             "them saw yours."]
    for op in opinions:
        if op.status == "ok":
            parts += [f"\n## Seat: {op.seat}", op.raw.strip()]
        else:
            parts.append(f"\n## Seat: {op.seat}\n(no opinion — the seat {op.status})")
    return "\n".join(parts)
