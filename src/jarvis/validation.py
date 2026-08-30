"""The validation panel: five profiled seats judge one submission, and a veto table in
code decides what their objections force.

A work order settles on its own word today — the least independent opinion available. This
module is the reviewer that never met the worker: it reads an `evidence.EvidencePacket` (the
diff, the files, the submitter's declared testing evidence) and returns one outcome for the
round machine to act on. It is called; it is never messaged, and it messages nobody.

## The contract, exactly

    decide(store, round_row, packet, cfg) -> {
        "outcome": "passed" | "rejected" | "escalated",
        "reason":  str,     # <= 1500 chars, second person, addressed to the submitter,
                            # empty ONLY when the outcome is "passed"
        "seats":   [{"seat", "status", "verdict", "reply", "model", "latency_ms"}, ...],
    }

Raising `claude_cli.ClaudeCliError` means total failure: `Daemon._validate_work_order`
catches it, marks the round `failed` — which `counted_validation_rounds` ignores — and
retries on the next tick without the submitter paying a round for a network outage.

## What must not drift

**NOTHING FORCES A PASS.** `arbitrate` has exactly one `return` that is not None and its
outcome is `"rejected"`, asserted by an AST walk in `tests/test_validation_arbitrate.py`.
A panel where agreement could be manufactured is a panel that adds latency and nothing else.

**`security` AND `tester` HOLD A VETO; `architect` AND `maintainer` HOLD NONE.** Their
failure mode is an annoying rejection loop, which spends exactly the attention this feature
exists to save — the mirror of the `taste` seat in Neo's panel. The mandates say so in as
many words, and `tests/test_validation_seats.py` asserts the prose against the shipped
markdown: a seat told it can block, by a table that says it cannot, is the exact failure
this design lineage exists to prevent.

**THIS MODULE IMPORTS NEITHER `neo`, `neo_store`, `panel` NOR `bus`**, function bodies
included, and a test walks the AST to keep it that way. The two panels share `seats.py` and
nothing else — in particular NOT a learnings ledger, because `neo_store.learnings` is one
OS-wide table whose vocabulary also contains `chair`, so a ruling the user taught Neo's
chair would silently start steering validation verdicts. The seats read the PROJECT's
knowledge base (`jarvis learn add --project …`) instead, which is where a user's standards
for a codebase actually live.

**THE SEATS JUDGE THE PACKET AND ONLY THE PACKET** — `cwd = $JARVIS_HOME`, `tools=""`. A
headless call carries no settings file, so what a tooled seat could reach would depend on
the user's global configuration rather than on anything Jarvis controls.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Mapping, Sequence
from typing import TYPE_CHECKING, Any

from . import claude_cli, seats, structured
from .bootstrap import ASSETS
from .project_store import VALIDATOR_SEATS

if TYPE_CHECKING:  # pragma: no cover - typing only
    from .catalog import ValidationConfig
    from .central_store import KnowledgeBrief
    from .evidence import EvidencePacket
    from .project_store import ProjectStore

log = logging.getLogger("jarvis.validation")

#: Where the seat definitions live. DELIBERATELY NOT `assets/agents/`: `bootstrap._rebuild`
#: copytrees that directory wholesale into every feature-order planner's `.claude/agents/`,
#: so a seat dropped there becomes a bogus subagent every planner session can invoke.
SEAT_ASSETS = ASSETS / "validator-seats"

#: First line of every seat's system prompt, and A DIFFERENT LITERAL FROM
#: `panel.SEAT_HEADER` on purpose. `chair` is a legal seat name in both rosters, so a
#: shared header would leave nothing — not the test fake, not a reader of the record —
#: able to tell a validation chair's call from a Neo chair's.
SEAT_HEADER = "# Jarvis validation seat: {seat}"

#: The seats whose objection FORCES a rejection, in the order their reason is preferred
#: when both of them block. Security first: an exposure the submitter has not seen is the
#: thing they most need to read first.
#:
#: `architect` AND `maintainer` ARE ABSENT DELIBERATELY, and their absence is the sharpest
#: thing in this module — the negative control of the whole table. A checker that treated
#: every objection alike, without asking WHICH SEAT raised it, would pass every other row
#: and get these two wrong silently, in the direction that spends the user's time.
VETO_SEATS = ("security", "tester")

#: How much of the reviewer's message survives to the submitter. The round machine frames
#: it (`daemon.REVIEW_FEEDBACK`) and the bus frames that again, so a reason that ran on
#: would push the instructions that matter off the bottom of the message.
REASON_LIMIT = 1500

#: What the submitter reads when the panel refused but nobody wrote a sentence.
UNSTATED_REJECTION = ("the review was not satisfied with this submission, and the seat "
                      "that refused it did not say why.")


def roster() -> seats.Roster:
    """The validation roster, as `seats.py` sees it.

    Built per call rather than held as a constant, exactly as `panel.neo_roster` is: a
    test swaps `SEAT_ASSETS` to unship a seat, and a roster captured at import time would
    ignore the swap. The cache in `seats.definition` is keyed on this object, so the two
    rosters' `chair.md` files cannot answer for one another.
    """
    return seats.Roster(assets=SEAT_ASSETS, vocabulary=VALIDATOR_SEATS,
                        header=SEAT_HEADER)


def definition(seat: str) -> tuple[dict[str, str], str]:
    """The shipped (frontmatter, mandate) for one validation seat."""
    return seats.definition(roster(), seat)


def shipped_seats() -> tuple[str, ...]:
    return seats.shipped(roster())


def seat_model(seat: str, cfg: ValidationConfig) -> str:
    """Which model this seat runs on, or `""` for "whatever the CLI defaults to".

    Most specific wins: the catalog's per-seat map, then `chair_model` for the chair, then
    the definition's own `model:` key. There is no fourth step, and that is a deliberate
    reading of the seam: `Daemon._validator` is handed `os.validation` alone, and widening
    it to carry `default_model` would couple the panel to the whole OS config for one
    field. An empty string sends no `--model` flag at all.
    """
    explicit = cfg.seat_models.get(seat) or (cfg.chair_model if seat == "chair" else "")
    try:
        declared = definition(seat)[0].get("model", "")
    except seats.SeatError:
        declared = ""
    return explicit or declared or ""


# -- the prompts ------------------------------------------------------------------------


def render_knowledge(brief: KnowledgeBrief, project: str) -> list[str]:
    """The project's standing instructions, as a seat can use them.

    THE SUBSTRATE IS `CentralStore.knowledge` — what `jarvis learn add --project <p>`
    writes — and not `neo_store.learnings`. The two ledgers are fed by different acts: a
    learning is distilled from the user reviewing NEO'S ANSWERS, which says nothing about
    whether a diff was adequately tested, and it is keyed by a seat vocabulary that also
    contains `chair`. Sharing it would let a ruling taught to Neo's chair steer a
    validation verdict, with nothing on either side looking wrong.

    Same INDEX LINES as the worker prompt (`dispatch.render_knowledge_block`) — id,
    headline, global marker — because a second index format is a second thing to keep in
    step. The RETRIEVAL VERB is different and must be: a worker is told to run
    `jarvis learn show <id>`, and a seat has no tools, so pointing it at a command would
    be pointing it at a resource it cannot reach.
    """
    if not brief:
        return []
    lines = [
        "",
        f"# The project's standing instructions — {brief.total} entries for "
        f"`{project}`",
        "These are the user's own rules for this codebase, in their own words. They are "
        "STANDING INSTRUCTIONS, not background: a submission that contradicts one is a "
        "finding, whatever else is right about it.",
        "You have no tools and cannot fetch anything, so these headlines are all you get "
        "— judge on what is here and never invent an entry. When one of them decides "
        "your verdict, CITE ITS `kn-` ID in your reason: the id is stored with your "
        "opinion, so a rejection can be traced back to the instruction that caused it.",
    ]
    if brief.pinned:
        lines += ["", "## Always in force (full text)"]
        for k in brief.pinned:
            topic = f" [{k['topic']}]" if k["topic"] else ""
            lines.append(f"- ({k['project'] or 'global'}{topic}) {k['content']}")
    if brief.digest:
        lines += ["", "## Index — headline only"]
        current = object()
        for k in brief.digest:
            if k["topic"] != current:
                current = k["topic"]
                lines.append(f"### {k['topic'] or '(no topic)'}")
            scope = "" if k["project"] == project else " (global)"
            lines.append(f"- `{k['id']}`{scope} {k['headline']}")
    if brief.overflow:
        listed = ", ".join(f"{t or '(no topic)'} ({n})" for t, n in brief.overflow)
        lines += ["", f"## Not indexed above — {brief.overflow_count} further entries, "
                      f"by topic", listed]
    return lines


def build_seat_system_prompt(seat: str, project: str,
                             brief: KnowledgeBrief | None = None) -> str:
    """One seat's mandate plus the project's standing instructions. Byte-stable per seat.

    Stable because the prefix has to be identical call to call or every seat pays a full
    prompt-cache miss, and a round is five calls: what varies per submission rides in the
    user prompt, after this.
    """
    _, mandate = definition(seat)
    parts = [SEAT_HEADER.format(seat=seat), "", mandate]
    if brief is not None:
        parts += render_knowledge(brief, project)
    return "\n".join(parts)


def build_packet_prompt(packet: EvidencePacket) -> str:
    """The submission, as every seat reads it — the same bytes for all of them.

    `files`, `stat` and `dropped_files` are here even when the diff is complete, because
    they are what lets a seat say "you claim tests, and no file under `tests/` appears in
    this change" — an answer the diff alone cannot support once it has been truncated.

    When the unit carries a spec section, that section is the standard the change is held
    to and the brief is demoted to the scope boundary around it — the heading says so,
    because a seat handed two descriptions of the same work will otherwise pick whichever
    the diff agrees with. No section, and the prompt is exactly what it was before: §5 of
    docs/superpowers/specs/2026-08-29-spec-driven-feature-orders.md.
    """
    unit = "feature order" if packet.unit == "feature" else "work order"
    parts = [
        f"# The submission — {unit} {packet.subject_id}",
        f"## Title\n{packet.title}",
        f"## The brief it was given\n{packet.description or '(none recorded)'}",
    ]
    if packet.spec_section:
        parts.append(
            f"## THE SPEC THIS WAS BUILT TO — {packet.spec_ref}\n"
            f"This section is the source of truth for what the change was supposed to "
            f"be; the brief above is only the scope boundary around it. Judge whether "
            f"the diff implements THIS, and say which part of it is unimplemented, "
            f"contradicted or exceeded.\n\n{packet.spec_section}")
    parts += [
        f"## What the submitter says it did\n{packet.summary or '(nothing stated)'}",
        "## The testing evidence the submitter DECLARED\n"
        f"{packet.declared or '(none declared — the submitter claimed no evidence)'}",
    ]
    if packet.pr_url:
        parts.append(f"## Pull request\n{packet.pr_url}")
    if packet.children:
        parts.append("## What each child of this feature claimed")
        for child in packet.children:
            parts.append(
                f"### {child.get('id') or '?'} — {child.get('title') or ''}\n"
                f"summary: {child.get('summary') or '(none)'}\n"
                f"declared evidence: {child.get('declared') or '(none)'}")
    parts.append(f"## The change\n`{packet.base or '?'}` → `{packet.head or '?'}`")
    listed = "\n".join(f"- {f}" for f in packet.files) or "(no files changed)"
    parts.append(f"## Every file this change touches ({len(packet.files)}) — "
                 f"this list is NEVER truncated\n{listed}")
    if packet.stat:
        parts.append(f"## git diff --stat\n```\n{packet.stat}\n```")
    if packet.diff_truncated:
        dropped = "\n".join(f"- {f}" for f in packet.dropped_files) or "- (none)"
        parts.append(
            "## THE DIFF BELOW IS TRUNCATED — YOU HAVE NOT SEEN EVERYTHING\n"
            "It was cut at a file boundary. These files are in the change and their "
            f"patch is NOT below:\n{dropped}\n"
            "Judge what you can see, and say plainly that you could not see the rest "
            "rather than passing what you did not read.")
    parts.append(f"## The diff\n```diff\n{packet.diff or '(empty)'}\n```")
    return "\n\n".join(parts)


def build_chair_prompt(packet: EvidencePacket, opinions: Sequence[seats.Opinion]) -> str:
    """The submission, then every seat's reply verbatim.

    Verbatim rather than summarised: a summariser between the seats and the chair is one
    more place for the concrete ask a seat wrote to be silently softened.
    """
    parts = [build_packet_prompt(packet), "", "# The panel's opinions",
             "Each seat answered blind — none of them saw another's reply, and none of "
             "them saw yours. A seat with no opinion errored or timed out; it abstained, "
             "and silence is never agreement."]
    for op in opinions:
        if op.status == "ok":
            parts += [f"\n## Seat: {op.seat}", op.raw.strip()]
        else:
            parts.append(f"\n## Seat: {op.seat}\n(no opinion — the seat {op.status})")
    return "\n".join(parts)


# -- arbitration: the veto table, as code -------------------------------------------------


def _reply(op: Mapping[str, Any]) -> dict[str, Any]:
    """One opinion's reply as an object, or `{}` when the seat said nothing usable.

    `{}` covers abstained, failed, an unrecognised status and output that will not parse —
    all of which are SILENCE. Silence is not a veto and it is not consent: it produces no
    signal here at all, and the decision goes on to the chair, whose mandate says in as
    many words never to read silence as agreement.
    """
    if str(op.get("status") or "") != "ok":
        return {}
    data = structured.parse_json_object(str(op.get("reply") or ""))
    return data if isinstance(data, dict) else {}


def _raised(data: Mapping[str, Any], key: str) -> bool:
    """Did the seat raise this flag? Read PERMISSIVELY, on purpose.

    `bool()` rather than `is True`, so a model that wrote the string `"false"` blocks
    something it did not mean to. That is deliberate and it is the only direction this can
    be wrong in: every flag it reads points at a rejection, so a permissive read costs one
    rejection too many and a strict read costs one too few — and a rejection the submitter
    disagrees with costs a round, while a pass nobody meant to give costs the whole
    feature.
    """
    return bool(data.get(key))


def _asks(data: Mapping[str, Any]) -> list[str]:
    """The seat's concrete asks, as lines. A reply with none is not an error: the reason
    is allowed to be the whole of what the submitter must act on."""
    raw = data.get("asks")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(a).strip() for a in raw if str(a).strip()]


def _message(reason: str, asks: Sequence[str]) -> str:
    """The forcing seat's own words, verbatim and UNATTRIBUTED, plus its asks.

    Unattributed for the same reason Neo's panel does it: the reason is delivered to the
    submitter, and deliberation never leaves the room. Quoting what a seat said is the
    substance of the rejection; naming which seat said it would be narrating the panel.
    """
    text = reason.strip() or UNSTATED_REJECTION
    if asks:
        text += "\n\nWhat this needs before it can pass:\n" + "\n".join(
            f"- {a}" for a in asks)
    return text[:REASON_LIMIT]


def arbitrate(opinions: Sequence[Mapping[str, Any]]) -> dict[str, Any] | None:
    """The veto table. Returns the outcome the seats FORCED, or None for "let the chair
    decide".

    Pure: plain dicts in, an outcome or None out. No store, no model, no clock. Each
    opinion is `{"seat", "status", "reply"}` — deliberately the shape of a stored
    `validation_opinions` row, so the same arbitration can be replayed over the record as
    well as over what was just collected.

    THE ROWS:

    * `security` raising `blocking` forces `rejected`.
    * `tester` raising `blocking` forces `rejected`.
    * `architect` forces nothing, however it replies.
    * `maintainer` forces nothing, however it replies.
    * A seat outside the table forces nothing, whatever it calls itself.
    * The CHAIR'S OWN REPLY IS NEVER ARBITRATED. The chair is not a fifth objector; it
      synthesises, and it runs only when this returned None.
    * NOTHING FORCES A PASS — structurally, not by inspection: there is exactly one
      `return` here that is not None, and its outcome is `"rejected"`.

    Why this is not a paragraph in the chair's mandate: the one measured failure of this
    lineage on record was a persona whose clause ordering structurally forced the wrong
    answer. A safety rule that lives in prose is a rule that holds by prompt luck.
    """
    forcing: dict[str, str] = {}
    for op in opinions:
        seat = str(op.get("seat") or "")
        if seat not in VETO_SEATS:
            continue
        data = _reply(op)
        if not data or not _raised(data, "blocking"):
            continue
        forcing[seat] = _message(str(data.get("reason") or ""), _asks(data))

    if not forcing:
        return None

    reason = next((forcing[s] for s in VETO_SEATS if forcing.get(s)), "")
    return {"outcome": "rejected", "reason": reason or UNSTATED_REJECTION}


# -- the entry point ------------------------------------------------------------------------


def decide(store: ProjectStore, round_row: dict[str, Any], packet: EvidencePacket,
           cfg: ValidationConfig) -> dict[str, Any]:
    """Judge one submission. THE VALIDATOR, as `Daemon._validator` returns it.

    The shape of a decision, in order: one blind round over the non-chair seats, every
    opinion recorded, then `arbitrate`, and only if the seats forced nothing, the chair.

    EVERY PROMPT IS BUILT ON THIS THREAD, before anything fans out. A sqlite connection
    belongs to the thread that opened it, and `seats.run_blind` takes no store precisely
    so that a seat on a pool thread cannot reach one.
    """
    from .central_store import CentralStore
    from .paths import ensure_home

    round_id = int(round_row["id"])
    central = CentralStore()
    try:
        project = central.project_name_for_path(store.project_path)
        brief = central.knowledge_brief(project)
    finally:
        central.close()

    prompt = build_packet_prompt(packet)
    prompts: dict[str, tuple[str, str]] = {}
    missing: list[seats.Opinion] = []
    for seat in cfg.roster:
        if seat == "chair":
            continue
        try:
            prompts[seat] = (build_seat_system_prompt(seat, project, brief), prompt)
        except seats.SeatError as e:
            # Not an outage: this build ships no such seat. The panel proceeds without it
            # rather than stalling the round, and the row says `failed` rather than
            # `abstained` — a seat that CANNOT run is not one that timed out.
            log.error("validation seat %s cannot run: %s", seat, e)
            missing.append(seats.Opinion(seat=seat, raw=str(e), status="failed",
                                         replied=False))

    opinions = seats.run_blind(
        prompts, models={seat: seat_model(seat, cfg) for seat in prompts},
        timeout=cfg.timeout, cwd=ensure_home(), tools="")
    opinions += missing
    for op in opinions:
        _record(store, round_id, project, packet, op)

    if opinions and not any(op.replied for op in opinions):
        # EVERY SEAT WENT DOWN. The chair would then synthesise from four abstentions and
        # could pass work nothing judged, which is the one outcome this feature cannot
        # produce. Not a transport failure either — the calls happened and the round is a
        # real one — so a human is asked instead.
        return _out("escalated", "nobody could be reached to review this submission, so "
                                 "the work has not been judged.", opinions)

    forced = arbitrate([{"seat": op.seat, "status": op.status, "reply": op.raw}
                        for op in opinions])
    if forced is not None:
        # The seats settled it and the chair gets no vote on the safety rule. Skipping it
        # saves a call AND buys a rule no prompt can talk itself out of.
        log.info("validation: a veto seat rejected round %s; the chair was not run",
                 round_id)
        return _out("rejected", forced["reason"], opinions)

    if "chair" not in cfg.roster:
        return _out("escalated", "this panel has no chair, so nothing could turn the "
                                 "seats' opinions into a verdict.", opinions)

    chair = _run_chair(store, round_id, packet, opinions, cfg, project, brief)
    opinions = [*opinions, chair]
    data = chair.data or {}
    outcome = str(data.get("outcome") or "").strip().lower()
    reason = str(data.get("reason") or "").strip()
    if outcome == "passed":
        # `reason` is emptied rather than trusted: the contract says a pass carries none,
        # and a passing round's reason is read as feedback wherever it is rendered.
        return _out("passed", "", opinions)
    if outcome == "rejected":
        return _out("rejected", reason or UNSTATED_REJECTION, opinions)
    # No verdict this machine knows — an unparseable reply, or a word nobody defined.
    # FAILS TOWARD THE USER, never toward a pass.
    return _out("escalated", reason or "the review could not reach a verdict on this "
                                       "submission.", opinions)


def _out(outcome: str, reason: str, opinions: Sequence[seats.Opinion]) -> dict[str, Any]:
    """The contract's three keys, and the reason capped where the contract caps it.

    The verdict is narrowed HERE as well as in `_record`, and that is not belt-and-braces:
    the round machine re-records every seat from this list, and it asserts the store's
    vocabulary. A word this module accepted but the store refuses would raise in the
    daemon, after the judgement had been paid for.
    """
    return {"outcome": outcome, "reason": reason[:REASON_LIMIT],
            "seats": [{**op.summary(), "verdict": _verdict(op.verdict), "reply": op.raw}
                      for op in opinions]}


def _verdict(word: str) -> str:
    """One seat's verdict, narrowed to the vocabulary the store will accept.

    `record_validation_opinion` asserts on `VALIDATION_VERDICTS`, so a model that answered
    "passed" where the schema said "pass" would raise INSIDE the round and take down a
    judgement that had already been paid for. Anything unrecognised is recorded as no
    verdict at all — which is what it is — and the raw reply is stored beside it either
    way, so nothing is lost.
    """
    word = (word or "").strip().lower()
    if word.startswith("pass"):
        return "pass"
    if word.startswith("reject"):
        return "reject"
    return ""


def _record(store: ProjectStore, round_id: int, project: str,
            packet: EvidencePacket, op: seats.Opinion) -> None:
    """Persist one seat's contribution: what it said, and what it cost.

    ONE `agent_calls` ROW PER SEAT, never one per round. Whether the panel earns its price
    is exactly the question of what five seats cost against the review they replace, and
    an aggregate cannot answer it — nor say which seat is the expensive one. A seat that
    never replied has no usage to record, so it gets no row: it cost nothing.
    """
    from . import agent_usage

    store.record_validation_opinion(round_id, op.seat, reply=op.raw,
                                    verdict=_verdict(op.verdict), status=op.status,
                                    model=op.model, latency_ms=op.latency_ms)
    if op.usage is None:
        return
    agent_usage.record("validation_seat", usage=op.usage, label=op.seat,
                       model=op.model, project=project,
                       wo_id=packet.subject_id if packet.unit != "feature" else "",
                       ok=op.status == "ok")


def _run_chair(store: ProjectStore, round_id: int, packet: EvidencePacket,
               opinions: Sequence[seats.Opinion], cfg: ValidationConfig, project: str,
               brief: KnowledgeBrief) -> seats.Opinion:
    """Synthesise. The chair is the one seat that is not blind — that is its whole job.

    A chair that cannot be reached is TOTAL FAILURE, not a seat abstaining: there is no
    verdict without it. The abstention is recorded first so the deliberation survives the
    exception, then `ClaudeCliError` propagates and the round machine retries the round
    without the submitter paying for it.
    """
    from .paths import ensure_home

    model = seat_model("chair", cfg)
    system = build_seat_system_prompt("chair", project, brief)
    prompt = build_chair_prompt(packet, opinions)
    started = time.monotonic()
    try:
        result = claude_cli.run_headless_result(prompt, system_prompt=system, model=model,
                                                timeout=cfg.timeout, cwd=ensure_home(),
                                                tools="", attribute=False)
    except claude_cli.ClaudeCliError as e:
        op = seats.Opinion(seat="chair", raw=str(e), status="abstained", model=model,
                           replied=False,
                           latency_ms=int((time.monotonic() - started) * 1000))
        _record(store, round_id, project, packet, op)
        raise
    data = structured.parse_json_object(result.text)
    # `failed`, not `ok`, when the reply will not parse — the same rule `seats._run_seat`
    # applies to every other seat, and the row is what a later reader has to tell "the
    # chair judged this" from "the chair said something nobody could read".
    op = seats.Opinion(seat="chair", raw=result.text, model=model, usage=result.usage,
                       latency_ms=int((time.monotonic() - started) * 1000),
                       status="ok" if isinstance(data, dict) else "failed",
                       verdict=str((data or {}).get("outcome") or "").strip())
    _record(store, round_id, project, packet, op)
    return op
