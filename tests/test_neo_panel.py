"""Neo as a panel: the primitive, all five seats, the fast path and the veto arbitration.

The arbitration's own table lives in `tests/test_panel_arbitrate.py`, which is pure — plain
dicts in, a verdict out. What is here is everything that needs the mechanism around it: how
many calls a decision costs, which seat was handed which prompt and which model, what got
stored, and what reached the worker.

Every test here is MODEL-FREE — the seats' judgment is measured by the opt-in evals under
evals/llm/, never here. What is testable in-process is the MECHANISM: how many calls a
decision costs, which prompt each seat was handed, what got stored, and what reached the
worker.

TWO ASSERT-NOTHING TRAPS SHAPE THIS FILE, and both are easy to fall into:

* The panel ships DISABLED, so a test that reaches Neo through the daemon without
  explicitly enabling it exercises the single agent and still gets a perfectly good
  verdict. So the assertions are on CALL COUNTS and on `panel_opinions` rows — never on
  "a verdict came back".
* The fake `claude` returns a valid, non-escalating verdict for any prompt at all, and
  its gate branch returns a well-formed gate verdict with NO `route` key. A lenient
  `decide` would default that to `panel` and the test would pass having exercised
  nothing. `test_a_premise_reply_with_no_route_runs_the_panel` is the assertion that
  catches it.

The seat definitions are asserted THROUGH THE SHIPPED MARKDOWN, never against a Python
constant: the file the runtime reads is the enforcement, and a constant listing what the
seats say would keep passing forever while the markdown said something else.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from jarvis import neo as neo_mod
from jarvis import ops, panel
from jarvis.bootstrap import ASSETS
from jarvis.catalog import (
    DEFAULT_PANEL_TIMEOUT,
    NeoConfig,
    PanelConfig,
    load_catalog,
)
from jarvis.central_store import CentralStore
from jarvis.daemon import Daemon
from jarvis.neo_store import SEATS, NeoStore
from jarvis.project_store import ProjectStore

SEAT_DIR = ASSETS / "neo-seats"

# Every seat, read off the SHIPPED DIRECTORY rather than listed here. A Python list of
# what is supposed to be on disk keeps passing forever while the disk says something else,
# and the enforcement that matters is the file the runtime actually reads.
SHIPPED = tuple(sorted(p.stem for p in SEAT_DIR.glob("*.md")))

#: The full roster. Not `neo_store.SEATS` — that is the VOCABULARY, and the two are
#: different things on purpose: a roster may name a seat whose markdown arrives in a later
#: release. They coincide today, and `test_every_seat_in_the_vocabulary_now_ships` is where
#: that is asserted rather than assumed.
FULL_ROSTER = ("premise", "record", "blast", "taste", "chair")

GATE_QUESTION = (
    "PRIVILEGED ACTION REQUEST\n\n"
    "The worker for work order wo-1 tried to run:\n\n"
    "    uv run pytest tests/test_release_staging.py -k shipit\n\n"
    "Why: running the release-staging tests.\n"
)


# -- fixtures ---------------------------------------------------------------------------


@pytest.fixture()
def store(jarvis_home):
    s = NeoStore()
    try:
        yield s
    finally:
        s.close()


def cfg(**panel_kwargs) -> NeoConfig:
    """A Neo config with the panel ON. Nothing in the OS does this by default."""
    panel_kwargs.setdefault("enabled", True)
    return NeoConfig(panel=PanelConfig(**panel_kwargs))


def claim(store: NeoStore, question: str, kind: str = "question") -> dict:
    store.ask("proj_a", "wo-1", question, context="build the exporter", kind=kind)
    q = store.claim_next()
    assert q is not None
    return q


def headless(fake_claude) -> list[dict]:
    """Every one-shot `claude -p` call: Neo's and the panel's. Worker turns carry a
    session id and are excluded, exactly as `tests/test_neo.py` does it."""
    return [c for c in fake_claude.calls
            if "-p" in c["argv"]
            and "--session-id" not in c["argv"] and "--resume" not in c["argv"]]


def arg(call: dict, flag: str) -> str:
    argv = call["argv"]
    return argv[argv.index(flag) + 1] if flag in argv else ""


def seat_of(call: dict) -> str | None:
    system = arg(call, "--append-system-prompt")
    return next((s for s in SEATS if f"# Neo panel seat: {s}" in system), None)


# -- the seat definitions, as shipped ----------------------------------------------------


@pytest.mark.parametrize("seat", SHIPPED)
def test_a_seat_ships_as_markdown_with_frontmatter(seat):
    meta, body = panel.parse_definition((SEAT_DIR / f"{seat}.md").read_text())

    assert meta["name"] == seat, "the roster resolves a seat by its file and its name key"
    assert meta["name"] in SEATS, (
        "a seat file naming something outside the vocabulary can never be rostered: the "
        "catalog validator rejects the name before the file is ever read"
    )
    assert meta["description"], "a seat with no description is undocumented in the record"
    assert body.strip(), "a seat is its mandate; an empty body is an empty seat"
    # A `tools:` key is meaningful for a subagent and meaningless for a headless call.
    # Absent rather than empty: an empty allowlist would read as a deliberate lockdown.
    assert "tools" not in meta


def test_every_seat_in_the_vocabulary_now_ships():
    """The vocabulary and the shipped set were deliberately allowed to differ while the
    panel was being built in pieces. They no longer do, and saying so here is what stops
    the parametrized test above from silently covering four seats if a file is lost."""
    assert set(SHIPPED) == set(SEATS) == set(FULL_ROSTER)
    assert panel.shipped_seats() == SEATS


def test_the_seats_do_not_ship_in_the_planners_agents_directory():
    """`bootstrap._rebuild` copytrees `assets/agents/` WHOLESALE into every feature-order
    planner's `.claude/agents/`. A Neo seat dropped there — subdirectory or not — becomes
    a bogus subagent that every planner session can invoke.

    Checked over the WHOLE vocabulary, not over the two seats that shipped first: three
    more arrived after this test was written, and a per-name list is exactly the thing
    that does not notice a fourth.
    """
    assert SEAT_DIR.is_dir(), "the seats must ship somewhere"
    assert not [p for p in (ASSETS / "agents").rglob("*")
                if any(s in p.name for s in SEATS)]


def test_the_premise_seat_carries_the_hard_limit_verbatim():
    """The one thing that keeps a mis-dismissal from being an open door. This wording
    lives in `gates.REVIEWER_PERSONA` today, and the premise seat is where it moves — a
    seat that owns proposing `dismiss` and does not carry the limit on dismissing is a
    seat that dismisses real releases."""
    body = " ".join((SEAT_DIR / "premise.md").read_text().split())

    assert "however routine or well-justified it looks" in body
    assert "assume the privileged reading" in body


def test_the_premise_check_comes_before_the_first_approve_clause():
    """Position is the fix, not the wording — this is the whole reason the seat exists.

    The old single persona OPENED by asserting a worker had run a command that ships code
    and then required of every approval that work "landed on a branch, in a pull request,
    with checks passing", which a misclassified `grep` can never satisfy: the closest
    fitting clause left was DENY. "Was this even the question that was asked" has to be
    settled before any question about the worker.
    """
    body = (SEAT_DIR / "premise.md").read_text()

    assert body.index("PREMISE CHECK") < body.index("APPROVE")


def test_the_premise_seat_is_told_to_emit_a_route():
    """On `route=fast` this seat's reply is the final verdict and the chair never runs.
    A seat that does not know it routes costs four extra calls on every decision."""
    body = (SEAT_DIR / "premise.md").read_text()

    assert '"route"' in body and "fast" in body and "panel" in body


def collapsed(seat: str) -> str:
    """A seat's mandate as one lower-cased line. The mandates are hard-wrapped prose and
    every sentence worth pinning straddles a line break."""
    return " ".join((SEAT_DIR / f"{seat}.md").read_text().lower().split())


def test_the_blast_seat_carries_the_hard_limit_and_the_evidence_check():
    """`blast` is the seat that has to hold the hard limit AFTER the premise seat has
    already proposed letting the command through — it is the only seat allowed to overrule
    it, so a copy of the limit lives here as well as there.

    The evidence check is asserted in the same test because there is deliberately no
    evidence seat: if it is not in this mandate it is in nobody's.
    """
    body = collapsed("blast")

    assert "however routine or well-justified it looks" in body
    assert "assume the privileged reading" in body
    assert "non-vacuous" in body and "was ci actually green" in body


def test_the_blast_seat_is_told_its_veto_is_one_way():
    """The mandate has to agree with `arbitrate`, or the seat spends its replies reaching
    for a power the code does not give it. Belt and braces: the code is the enforcement
    (`test_nothing_can_force_an_approval`), and this is the seat being told so."""
    body = collapsed("blast")

    assert "you may never force an approval" in body
    assert "a veto is not a denial" in body
    assert "your silence is not consent" in body


def test_the_taste_seat_is_told_it_has_no_veto_and_no_forcing_power():
    """Its failure mode is an annoying answer, not a dangerous one. A seat that could block
    on taste would spend exactly the attention it exists to protect — and a seat that
    believes it can block will keep trying to, wasting a reply per decision."""
    body = collapsed("taste")

    assert "you have no veto and no forcing power" in body
    assert "do not emit a `verdict` key and do not emit a `veto` key" in body
    assert "an opinion the chair weighs" in body


def test_the_taste_seat_owns_the_answer_budget_and_scope_discipline():
    body = collapsed("taste")

    assert "at most 50 words" in body
    assert "never bundle" in body
    assert "exempt" in body, "the verbatim obligations survive the budget it enforces"


def test_the_record_seat_names_the_contradiction_before_the_verdict():
    """Position is the mandate. A contradiction found after a verdict has been reasoned out
    becomes a caveat on an answer already decided; the same one found first changes it."""
    body = (SEAT_DIR / "record.md").read_text()

    assert body.index("BEFORE THE VERDICT") < body.index("# OUTPUT")
    assert "unresolvable" in body and "resolvable" in body


def test_the_record_seat_has_retraction_as_a_real_remedy():
    """Retraction shipped after this seat was designed. Without it the seat could only
    complain about a stale ruling; with it, naming the entry is a fix the chair can carry
    to the user."""
    body = collapsed("record")

    assert "retract" in body
    assert "you retract nothing yourself" in body
    assert "naming an entry for retraction is not a way around it" in body


def test_the_record_seat_owns_the_verbatim_obligations():
    """Compliance phrasing is precisely what a summariser drops — it reads like padding
    right up to the moment it is missing. The budget is the chair's and the taste seat's;
    the exemption has to be somebody's job to surface."""
    body = collapsed("record")

    assert "verbatim" in body
    assert "exempt from the chair's budget" in body


@pytest.mark.parametrize("seat", ("record", "blast"))
def test_a_forcing_seat_is_told_its_reason_may_be_what_the_user_reads(seat):
    """Panel deliberation is stored and inspectable, and never pushed. On a forced
    escalation the chair does not run and the forcing seat's own `reason` IS what the user
    reads, so "name no seat" has to be in the mandate of the seats that can force it — not
    only in the chair's.

    `taste` is excluded rather than forgotten: it can force nothing, so its words never
    reach a user except through the chair, which carries the instruction already.
    """
    body = collapsed(seat)

    assert "name no seat" in body or "do not name the seats" in body


def test_the_chair_is_told_not_to_narrate_the_panel():
    """Panel deliberation is stored and inspectable on demand, and NEVER pushed. The
    chair's output is the whole of what a worker and a user receive, so the instruction
    has to be in the mandate, not just in the code that does not forward it."""
    # Collapsed, because the mandate is hard-wrapped prose and the sentences under test
    # straddle line breaks.
    body = " ".join((SEAT_DIR / "chair.md").read_text().lower().split())

    assert "do not name the seats" in body
    assert "they are never pushed" in body


# -- the seam: neo must never know panels exist -------------------------------------------


def test_neo_never_imports_the_panel():
    """Implemented as the design words it — `answer_question` becoming a caller of the
    panel, with a fallback to the single agent — this is an import cycle, in a codebase
    that has none. The seam is `drain_queue(answer=…)` instead.

    Walked as an AST rather than checked in `sys.modules`: this codebase's style is lazy
    imports inside function bodies, which a loaded-modules check cannot see.
    """
    tree = ast.parse(Path(neo_mod.__file__).read_text())
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported += [a.name for a in node.names]
        elif isinstance(node, ast.ImportFrom):
            imported += [a.name for a in node.names] + [node.module or ""]

    assert not any("panel" in name.split(".") for name in imported), imported


def test_drain_queue_answers_through_the_injected_callable(store, fake_claude):
    """The seam itself, with no panel involved: whatever the daemon passes is what
    answers, and passing nothing is the single agent."""
    store.ask("proj_a", "wo-1", "which delimiter?")
    seen = []

    def answer(store_, q, model, learnings_limit):
        seen.append((q["question"], model, learnings_limit))
        return {"escalate": False, "answer": "tab", "reason": "r", "verdict": "denied",
                "approve": False, "dispatch": None}

    neo_mod.drain_queue(store, model="opus", answer=answer)

    assert seen == [("which delimiter?", "opus", 50)]
    assert not headless(fake_claude), "the injected answerer replaced the model call"


# -- the contract at the boundary ----------------------------------------------------------


def test_decide_returns_the_verdict_shape_plus_exactly_one_key(store, fake_claude):
    """Nothing downstream needs to know a panel ran. `deliver`, the gate path and the plan
    path all read by key, so ADDING one is safe and changing the shape is not."""
    q = claim(store, "which delimiter?")

    result = panel.decide(store, q, cfg())

    assert set(result) == set(neo_mod.parse_verdict('{"escalate": false}')) | {"panel"}
    assert result["panel"]["route"] == "panel"


def test_every_seats_contribution_is_recorded(store, fake_claude):
    q = claim(store, "which delimiter?")

    panel.decide(store, q, cfg())

    rows = {r["seat"]: r for r in store.opinions(q["id"])}
    assert set(rows) == {"premise", "chair"}
    assert all(r["status"] == "ok" and r["reply"] for r in rows.values())
    assert rows["premise"]["reply"] != rows["chair"]["reply"]
    assert rows["premise"]["route"] == "panel"


# -- blindness -----------------------------------------------------------------------------


def test_the_seats_run_blind_and_only_the_chair_sees_them(store, fake_claude):
    """R3, diffusion of responsibility: a panel that agrees with itself is an expensive
    single agent. Agreement is only evidence if no seat could see another's answer.

    The chair is the deliberate exception — synthesising is its entire job — and asserting
    that in the same test is what stops the first half from passing for a run in which the
    seats produced nothing to leak.
    """
    q = claim(store, "which delimiter?")
    panel.decide(store, q, cfg())

    replies = {r["seat"]: r["reply"] for r in store.opinions(q["id"])}
    for call in headless(fake_claude):
        seat = seat_of(call)
        if seat in (None, "chair"):
            continue
        seen = arg(call, "-p") + arg(call, "--append-system-prompt")
        others = [s for s in replies if s != seat]
        assert not [s for s in others if replies[s] and replies[s] in seen], seat

    chair_call = next(c for c in headless(fake_claude) if seat_of(c) == "chair")
    assert replies["premise"] in arg(chair_call, "-p"), (
        "the chair synthesises; it has to be given what it is synthesising"
    )


def test_a_seats_system_prompt_is_its_own_mandate_and_its_own_learnings(store,
                                                                       fake_claude):
    """Byte-stable PER SEAT — each seat keeps its own cached prompt prefix, which is what
    makes running them in parallel free rather than a cache miss per seat. And the 50
    learnings stop being one shared budget: each seat carries only what it needs."""
    store.add_learning("Always default to CSV", project="proj_a")
    store.add_learning("A grep naming shipit ships nothing", project="proj_a",
                       seat="premise")
    q = claim(store, "which delimiter?")

    panel.decide(store, q, cfg())

    systems = {seat_of(c): arg(c, "--append-system-prompt") for c in headless(fake_claude)}
    assert "Always default to CSV" in systems["premise"]
    assert "A grep naming shipit ships nothing" in systems["premise"]
    # The seat-scoped learning is the premise seat's alone...
    assert "A grep naming shipit ships nothing" not in systems["chair"]
    assert "Always default to CSV" in systems["chair"]
    # ...and it never reaches the single agent, whose prefix has to stay byte-stable.
    assert "A grep naming shipit ships nothing" not in neo_mod.build_system_prompt(
        store, "proj_a")


# -- the fast path -------------------------------------------------------------------------


def test_a_dismissed_gate_costs_exactly_one_call(store, fake_claude):
    """The arithmetic the whole design turns on. ~95% of gate reviews are classifier false
    positives; if each cost a premise call AND a chair call, the panel would be a 2x cost
    and latency regression on Neo's highest-volume channel. On `route=fast` the premise
    seat's own reply IS the verdict."""
    q = claim(store, GATE_QUESTION + "FORCE_ROUTE_FAST FORCE_PROPOSE_DISMISS",
              kind="approval")

    result = panel.decide(store, q, cfg())

    calls = headless(fake_claude)
    assert len(calls) == 1
    assert seat_of(calls[0]) == "premise"
    assert result["panel"]["route"] == "fast"
    assert result["verdict"] == "dismissed"
    assert result["approve"] is False, "a dismissal clears a command; it authorises nothing"
    assert [r["seat"] for r in store.opinions(q["id"])] == ["premise"]


def test_an_approval_can_never_be_approved_on_the_fast_route(store, fake_claude):
    """The safety rule that pays for the fast path, and it is in CODE, not in a prompt.

    A real privileged action always costs the full panel — the fast route skips the seat
    that owns the blast radius, so the one verdict it must never be able to reach is the
    one that opens a gate.
    """
    q = claim(store, GATE_QUESTION + "FORCE_ROUTE_FAST FORCE_PROPOSE_APPROVE",
              kind="approval")

    result = panel.decide(store, q, cfg())

    assert result["panel"]["route"] == "panel"
    assert any(seat_of(c) == "chair" for c in headless(fake_claude)), (
        "the premise seat asked for the fast route and must not have got it"
    )
    assert result["approve"] is False
    assert result["verdict"] != "approved"


def test_the_rule_holds_however_the_premise_seat_routed():
    """The rule as a rule, not as a consequence of the routing above it. Written out so
    that relaxing the `dismiss`-only clause cannot silently open the gate as a side
    effect."""
    assert panel.fast_is_permitted("approval", "approved") is False
    assert panel.fast_is_permitted("approval", "dismissed") is True
    assert panel.fast_is_permitted("approval", "denied") is False
    assert panel.fast_is_permitted("approval", "") is False
    # An open question authorises nothing by construction, so it is always fast-eligible.
    assert panel.fast_is_permitted("question", "") is True
    assert panel.fast_is_permitted("question", "denied") is True


def test_an_open_question_on_the_fast_route_delivers_the_premise_seats_answer(
        store, fake_claude):
    q = claim(store, "FORCE_ROUTE_FAST which delimiter?")

    result = panel.decide(store, q, cfg())

    calls = headless(fake_claude)
    assert len(calls) == 1 and seat_of(calls[0]) == "premise"
    assert result["panel"]["route"] == "fast"
    premise_reply = json.loads(store.opinions(q["id"])[0]["reply"])
    assert result["answer"] == premise_reply["answer"]


def test_the_panel_route_calls_the_chair(store, fake_claude):
    q = claim(store, "which delimiter?")

    result = panel.decide(store, q, cfg())

    seats = [seat_of(c) for c in headless(fake_claude)]
    assert seats == ["premise", "chair"]
    chair_reply = json.loads(
        next(r for r in store.opinions(q["id"]) if r["seat"] == "chair")["reply"])
    assert result["answer"] == chair_reply["answer"]


def test_a_premise_reply_with_no_route_runs_the_panel(store, fake_claude):
    """Fail toward the expensive-but-safe side.

    This is also the assertion that catches a fake-`claude` collision: seat identity
    travels in `--append-system-prompt`, so a premise call on a gate question would
    otherwise hit the fake's "PRIVILEGED ACTION REQUEST" branch and come back as a
    well-formed gate verdict with no `route` key at all — after which a lenient `decide`
    defaults the route and the whole fast-path suite passes having exercised nothing.
    """
    q = claim(store, GATE_QUESTION + "FORCE_NO_ROUTE FORCE_PROPOSE_DISMISS",
              kind="approval")

    result = panel.decide(store, q, cfg())

    assert "route" not in json.loads(store.opinions(q["id"])[0]["reply"])
    assert result["panel"]["route"] == "panel"
    assert [seat_of(c) for c in headless(fake_claude)] == ["premise", "chair"]


def test_an_unparseable_premise_reply_runs_the_panel(store, fake_claude):
    q = claim(store, "FORCE_SEAT_GARBAGE FORCE_ROUTE_FAST which delimiter?")

    result = panel.decide(store, q, cfg())

    assert result["panel"]["route"] == "panel"
    assert [seat_of(c) for c in headless(fake_claude)] == ["premise", "chair"]


def test_the_fast_path_can_be_turned_off_entirely(store, fake_claude):
    q = claim(store, "FORCE_ROUTE_FAST which delimiter?")

    result = panel.decide(store, q, cfg(fast_path=False))

    assert result["panel"]["route"] == "panel"


# -- degradation ------------------------------------------------------------------------


def test_a_failed_premise_seat_falls_back_to_the_single_agent(store, fake_claude):
    """A Neo outage must never become a fleet stall. The seat that routes could not
    answer, so there is nothing to route on — and the single agent is a decision the OS
    already trusts.

    The system prompt is asserted BYTE-EQUAL to `neo.build_system_prompt`: the fallback
    has to be today's path exactly, not a paraphrase of it, or the fallback quietly
    becomes a third behaviour nobody measured.
    """
    fake_claude.fail_seat("premise")
    q = claim(store, "which delimiter?")

    result = panel.decide(store, q, cfg())

    calls = headless(fake_claude)
    expected = neo_mod.build_system_prompt(store, "proj_a", kind="question")
    single = [c for c in calls if arg(c, "--append-system-prompt") == expected]
    assert len(single) == 1, "exactly one single-agent call"
    assert len(calls) == 2, "the failed premise attempt, then the fallback"
    assert not any(seat_of(c) == "chair" for c in calls)
    assert result["panel"]["route"] == "single"
    assert result["answer"].startswith("neo-decision")
    # The abstention is still on the record: the seat ran and could not answer.
    assert [r["status"] for r in store.opinions(q["id"])] == ["abstained"]


@pytest.fixture()
def unship(monkeypatch, tmp_path):
    """Make a seat's definition not ship, for this test only.

    Every seat in the vocabulary now has a file, so the "a roster may name a seat this
    build has no markdown for" path can no longer be reached from the roster alone — and
    it is the path a catalog written ahead of a release actually takes. Rather than delete
    the case, the seat directory is swapped for a copy with one file left out.

    `definition` is `lru_cache`d, so the cache is cleared on the way in AND on the way out:
    a cached read of the real file would defeat the swap, and leaving the swapped miss
    cached would poison every later test in the session.
    """
    def hide(*seats: str):
        d = tmp_path / "neo-seats"
        d.mkdir(exist_ok=True)
        for s in SHIPPED:
            if s not in seats:
                (d / f"{s}.md").write_text((SEAT_DIR / f"{s}.md").read_text())
        monkeypatch.setattr(panel, "SEAT_ASSETS", d)
        panel.definition.cache_clear()

    panel.definition.cache_clear()
    yield hide
    panel.definition.cache_clear()


def test_a_seat_with_no_definition_shipped_does_not_stall_the_drain(store, fake_claude,
                                                                    unship):
    """A roster may name a seat whose markdown arrives in a later release — the catalog
    validates against the VOCABULARY (`neo_store.SEATS`), not against this build.

    Recorded `failed` rather than `abstained`, and that distinction is the point: a seat
    that CANNOT run is not one that timed out, and only the second is degradation.
    """
    unship("blast")
    q = claim(store, "which delimiter?")

    result = panel.decide(store, q, cfg(roster=("premise", "blast", "chair")))

    rows = {r["seat"]: r for r in store.opinions(q["id"])}
    assert rows["blast"]["status"] == "failed"
    assert "no definition ships" in rows["blast"]["reply"]
    assert rows["premise"]["status"] == "ok" and rows["chair"]["status"] == "ok"
    assert result["panel"]["route"] == "panel"


def test_a_failed_chair_is_total_failure(store, fake_claude):
    """`drain_queue` already rescues `ClaudeCliError` — it marks the question failed and
    tells the worker — so the panel must raise it rather than invent a verdict. There is
    no decision without the chair."""
    from jarvis import claude_cli

    fake_claude.fail_seat("chair")
    q = claim(store, "which delimiter?")

    with pytest.raises(claude_cli.ClaudeCliError):
        panel.decide(store, q, cfg())

    rows = {r["seat"]: r["status"] for r in store.opinions(q["id"])}
    assert rows == {"premise": "ok", "chair": "abstained"}, (
        "the deliberation survives the exception, or nobody can see why it failed"
    )


# -- models and timeouts -------------------------------------------------------------------


def test_a_seat_runs_on_neos_model_unless_the_catalog_says_otherwise(store, fake_claude):
    config = NeoConfig(model="opus", panel=PanelConfig(
        enabled=True, seat_models={"premise": "haiku"}, chair_model="sonnet"))
    q = claim(store, "which delimiter?")

    panel.decide(store, q, config)

    models = {seat_of(c): arg(c, "--model") for c in headless(fake_claude)}
    assert models == {"premise": "haiku", "chair": "sonnet"}
    assert panel.seat_model("chair", cfg()) == "opus", "chair falls back to Neo's model"
    assert panel.seat_model("premise", cfg()) == "opus"


def test_the_per_seat_timeout_reaches_the_model_call(store, monkeypatch):
    """`PanelConfig.timeout` is not decoration: without it every seat inherits the 300s
    default and one wedged seat holds the whole FIFO drain, and every worker parked behind
    it, for five minutes."""
    seen: list[int] = []

    def recorder(prompt, system_prompt=None, model=None, timeout=300, cwd=None,
                 tools=None, **kwargs):
        seen.append(timeout)
        return panel.claude_cli.HeadlessResult(
            text=json.dumps({"escalate": False, "answer": "a", "reason": "r"}))

    monkeypatch.setattr(panel.claude_cli, "run_headless_result", recorder)
    q = claim(store, "which delimiter?")

    panel.decide(store, q, cfg(timeout=17))

    assert seen == [17, 17]


def test_the_default_seat_timeout_is_well_below_neos_own():
    """Neo's call timeout is 300s and the seats run inside its single thread, so a
    per-seat timeout at or near it buys nothing: the drain would still stall for the full
    five minutes on one wedged seat."""
    assert DEFAULT_PANEL_TIMEOUT < 300 / 2


# -- the full roster -------------------------------------------------------------------------


def full(**panel_kwargs) -> NeoConfig:
    panel_kwargs.setdefault("roster", FULL_ROSTER)
    return cfg(**panel_kwargs)


def test_the_full_roster_on_the_fast_route_still_costs_exactly_one_call(store,
                                                                       fake_claude):
    """THE ARITHMETIC THE WHOLE DESIGN TURNS ON, and the reason `decide` runs the premise
    seat FIRST AND ALONE rather than fanning the roster out in one blind round.

    ~95% of gate reviews are classifier false positives. A single round over four seats
    would decide the route only after paying for the three seats the route exists to skip
    — turning the fast path from a 1-call answer into a 4-call one on the OS's
    highest-volume channel, which is worse than the 2x regression the fast path was
    introduced to prevent.
    """
    q = claim(store, GATE_QUESTION + "FORCE_ROUTE_FAST FORCE_PROPOSE_DISMISS",
              kind="approval")

    result = panel.decide(store, q, full())

    calls = headless(fake_claude)
    assert [seat_of(c) for c in calls] == ["premise"], "three seats and a chair skipped"
    assert result["panel"]["route"] == "fast"
    assert result["verdict"] == "dismissed" and result["approve"] is False
    assert [r["seat"] for r in store.opinions(q["id"])] == ["premise"]


def test_the_full_panel_runs_every_seat_and_then_the_chair(store, fake_claude):
    q = claim(store, "which delimiter?")

    result = panel.decide(store, q, full())

    seats = [seat_of(c) for c in headless(fake_claude)]
    assert seats[0] == "premise", "the seat that routes goes first"
    assert set(seats[1:-1]) == {"record", "blast", "taste"}
    assert seats[-1] == "chair"
    rows = {r["seat"]: r for r in store.opinions(q["id"])}
    assert set(rows) == set(FULL_ROSTER)
    assert all(r["status"] == "ok" and r["reply"] for r in rows.values())
    assert len({r["reply"] for r in rows.values()}) == len(FULL_ROSTER), (
        "five identical replies would pass every count assertion above having deliberated "
        "about nothing"
    )
    assert result["panel"]["route"] == "panel"


def test_the_later_seats_are_blind_to_the_premise_seat_that_ran_before_them(store,
                                                                           fake_claude):
    """Second in wall-clock order is not second in knowledge. The premise seat runs first
    so that the route can be decided before the rest are paid for — that ordering must not
    quietly turn the panel into a relay, where agreement is an echo rather than evidence."""
    q = claim(store, "which delimiter?")
    panel.decide(store, q, full())

    replies = {r["seat"]: r["reply"] for r in store.opinions(q["id"])}
    for call in headless(fake_claude):
        seat = seat_of(call)
        if seat in (None, "chair"):
            continue
        seen = arg(call, "-p") + arg(call, "--append-system-prompt")
        assert not [s for s in replies if s != seat and replies[s] and replies[s] in seen], seat

    chair_call = next(c for c in headless(fake_claude) if seat_of(c) == "chair")
    for seat in ("premise", "record", "blast", "taste"):
        assert replies[seat] in arg(chair_call, "-p"), (
            f"the chair synthesises; it was not shown what {seat} said"
        )


def test_each_seat_runs_on_its_own_model_and_the_rest_fall_back_to_the_fleet_default(
        store, fake_claude):
    """Most specific wins: the per-seat map, then `chair_model` for the chair, then Neo's
    model. The chair should be able to keep the expensive model when the seats do not — it
    is the one writing what a human reads."""
    config = NeoConfig(model="opus", panel=PanelConfig(
        enabled=True, roster=FULL_ROSTER, seat_models={"premise": "haiku"},
        chair_model="sonnet"))
    q = claim(store, "which delimiter?")

    panel.decide(store, q, config)

    models = {seat_of(c): arg(c, "--model") for c in headless(fake_claude)}
    assert models == {"premise": "haiku", "chair": "sonnet",
                      "record": "opus", "blast": "opus", "taste": "opus"}
    stored = {r["seat"]: r["model"] for r in store.opinions(q["id"])}
    assert stored == models, "what a seat ran on is part of the deliberation record"


def test_a_failed_seat_abstains_and_the_panel_proceeds_without_it(store, fake_claude):
    """A Neo outage must never become a fleet stall. Unlike the premise seat — which
    ROUTES, so its silence leaves nothing to route on and falls back to the single agent —
    a seat in the middle of the round is simply absent, and absence is not consent."""
    fake_claude.fail_seat("blast")
    q = claim(store, "which delimiter?")

    result = panel.decide(store, q, full())

    rows = {r["seat"]: r["status"] for r in store.opinions(q["id"])}
    assert rows == {"premise": "ok", "record": "ok", "blast": "abstained",
                    "taste": "ok", "chair": "ok"}
    assert any(seat_of(c) == "chair" for c in headless(fake_claude)), (
        "the chair still ran; an abstaining seat is not a forced outcome"
    )
    assert result["escalate"] is False
    assert result["panel"]["route"] == "panel"


# -- arbitration, end to end through `decide` ----------------------------------------------


def test_a_forced_escalation_skips_the_chair_entirely(store, fake_claude):
    """The arbitration is not advice to the chair, it is the decision. Asserting the chair
    NEVER RAN is what distinguishes "the code forced this" from "the chair happened to
    agree" — and the second is the prompt luck this feature exists to remove."""
    q = claim(store, "FORCE_RADIUS_ESCALATE which delimiter?")

    result = panel.decide(store, q, full())

    seats = [seat_of(c) for c in headless(fake_claude)]
    assert "chair" not in seats
    assert set(seats) == {"premise", "record", "blast", "taste"}
    assert "chair" not in {r["seat"] for r in store.opinions(q["id"])}
    assert result["escalate"] is True and result["approve"] is False
    assert result["reason"] == "test-forced escalation on the cost of being wrong"
    assert not [s for s in SEATS if s in result["reason"].lower()], (
        "the escalation quotes what was found and never who found it"
    )


def test_a_veto_turns_a_proposed_dismissal_into_an_escalation(store, fake_claude):
    """The row that matters most in practice: the premise seat proposed clearing a gated
    command, and the seat that owns the blast radius says it really does run the thing.

    The proposal is DEMOTED — to the user, not to a denial that tells a worker it
    misbehaved over an OS bug, and above all not to the approval it was raised against.
    """
    q = claim(store, GATE_QUESTION + "FORCE_PROPOSE_DISMISS FORCE_RADIUS_VETO",
              kind="approval")

    result = panel.decide(store, q, full())

    assert result["escalate"] is True
    assert result["verdict"] != "dismissed" and result["verdict"] != "approved"
    assert result["approve"] is False
    assert "chair" not in [seat_of(c) for c in headless(fake_claude)]


def test_an_unresolvable_contradiction_forces_the_escalation(store, fake_claude):
    q = claim(store, "FORCE_LEDGER_CONTRADICTION which delimiter?")

    result = panel.decide(store, q, full())

    assert result["escalate"] is True
    assert result["reason"] == "test-forced unresolvable contradiction"
    assert "chair" not in [seat_of(c) for c in headless(fake_claude)]


def test_the_premise_seat_escalating_on_the_panel_route_forces_it_too(store, fake_claude):
    """Ruled by Neo on question 59: the veto table enumerates forcing powers, not
    prohibitions, and escalate is the fail-safe direction, so the design's own line that
    "any seat that later returns escalate wins" governs. `taste` stays the exception, and
    the test below is where that is pinned."""
    q = claim(store, "FORCE_FRAME_ESCALATE which delimiter?")

    result = panel.decide(store, q, full())

    assert result["escalate"] is True
    assert "chair" not in [seat_of(c) for c in headless(fake_claude)]


def test_the_taste_seat_objecting_does_not_stop_the_chair(store, fake_claude):
    """THE NEGATIVE CONTROL, END TO END. The taste seat escalates AND vetoes, as loudly as
    its reply shape allows, and the panel carries on to the chair regardless.

    Its objection is stored in the same breath, which is what stops this passing for a run
    in which the seat never opined: the seat spoke, it was recorded, and it forced nothing.
    """
    q = claim(store, "FORCE_INTENT_ESCALATE which delimiter?")

    result = panel.decide(store, q, full())

    rows = {r["seat"]: r for r in store.opinions(q["id"])}
    assert json.loads(rows["taste"]["reply"])["escalate"] is True, "it did object"
    assert rows["chair"]["status"] == "ok", "and the chair ran anyway"
    assert result["escalate"] is False
    assert result["answer"] == json.loads(rows["chair"]["reply"])["answer"]


# -- end to end, through the daemon, with the panel enabled ---------------------------------


@pytest.fixture()
def panel_daemon(jarvis_home, fake_claude, tmp_path, project, claude_json):
    """The daemon with `os.neo.panel.enabled = true`. Nothing else in the suite does
    this; the default catalog leaves the panel off."""
    path = tmp_path / "catalog-panel.json"
    path.write_text(json.dumps({
        "os": {"defaults": {"model": "sonnet"},
               "notifications": {"sinks": ["log"]},
               "neo": {"panel": {"enabled": True}}},
        "projects": [{"name": "proj_a", "path": str(project),
                      "description": "test project"}],
    }))
    ops.start_os(str(path), foreground=True)
    return Daemon(load_catalog(path))


def test_the_deliberation_never_reaches_the_worker_or_the_inbox(panel_daemon, project,
                                                                fake_claude):
    """The design's hard line, end to end, on a real panel run.

    BOTH HALVES MATTER. Without the opinions assertion, "the message names no seat" passes
    perfectly for a run in which nothing deliberated at all — which is exactly what a
    default-configured OS does, since the panel ships disabled.
    """
    daemon = panel_daemon
    wo = ops.create_work_order("proj_a", "build the exporter")
    daemon.tick()
    ops.ask_question(wo["id"], "Should the export default to CSV or JSON?")

    daemon._neo_drain()

    neo = NeoStore()
    try:
        opinions = {r["seat"]: r for r in neo.opinions(1)}
        assert set(opinions) == {"premise", "chair"}, "the seats did not deliberate"
        assert all(o["status"] == "ok" for o in opinions.values())
        assert opinions["premise"]["reply"] != opinions["chair"]["reply"]
        chair_answer = json.loads(opinions["chair"]["reply"])["answer"]
    finally:
        neo.close()

    store = ProjectStore(project)
    try:
        messages = store.queued_messages(wo["id"])
    finally:
        store.close()
    assert len(messages) == 1
    assert messages[0]["content"] == f"{neo_mod.ANSWER_PREFIX} {chair_answer}"
    assert not [s for s in SEATS if s in messages[0]["content"]]

    central = CentralStore()
    try:
        rows = central.unacked_inbox()
    finally:
        central.close()
    assert not [s for s in SEATS
                for i in rows if s in f"{i['title']} {i.get('body') or ''}"]


@pytest.fixture()
def full_panel_daemon(jarvis_home, fake_claude, tmp_path, project, claude_json):
    """The daemon with the WHOLE roster rostered. The fixture above runs the shipped
    default (`premise` + chair); this is the configuration the three new seats only ever
    run under."""
    path = tmp_path / "catalog-full-panel.json"
    path.write_text(json.dumps({
        "os": {"defaults": {"model": "sonnet"},
               "notifications": {"sinks": ["log"]},
               "neo": {"panel": {"enabled": True, "roster": list(FULL_ROSTER)}}},
        "projects": [{"name": "proj_a", "path": str(project),
                      "description": "test project"}],
    }))
    ops.start_os(str(path), foreground=True)
    return Daemon(load_catalog(path))


def test_a_full_panels_deliberation_never_reaches_the_worker(full_panel_daemon, project,
                                                             fake_claude):
    """The design's hard line, on a run where all four seats really did deliberate.

    BOTH HALVES MATTER, and here the first one is load-bearing in a way it was not with
    two seats: without the opinions assertion, "the message names no seat" passes
    perfectly for a run in which `record`, `blast` and `taste` never opened their mouths.
    """
    daemon = full_panel_daemon
    wo = ops.create_work_order("proj_a", "build the exporter")
    daemon.tick()
    ops.ask_question(wo["id"], "Should the export default to CSV or JSON?")

    daemon._neo_drain()

    neo = NeoStore()
    try:
        opinions = {r["seat"]: r for r in neo.opinions(1)}
        assert set(opinions) == set(FULL_ROSTER), "the full panel did not deliberate"
        assert all(o["status"] == "ok" for o in opinions.values())
        assert len({o["reply"] for o in opinions.values()}) == len(FULL_ROSTER)
        chair_answer = json.loads(opinions["chair"]["reply"])["answer"]
    finally:
        neo.close()

    store = ProjectStore(project)
    try:
        messages = store.queued_messages(wo["id"])
    finally:
        store.close()
    assert len(messages) == 1
    assert messages[0]["content"] == f"{neo_mod.ANSWER_PREFIX} {chair_answer}"
    assert not [s for s in SEATS if s in messages[0]["content"].lower()]


def test_a_forced_escalation_reaches_the_inbox_naming_no_seat(full_panel_daemon, project,
                                                              fake_claude):
    """The path a forced escalation actually travels, and the one where a leak would be
    most likely: the chair does not run, so the line the user reads is a seat's own words,
    quoted by code rather than rewritten by a model.

    The control is the last assertion — the deliberation IS on file and readable — which
    is what proves the seats were absent from the inbox rather than absent full stop.
    """
    daemon = full_panel_daemon
    wo = ops.create_work_order("proj_a", "build the exporter")
    daemon.tick()
    ops.ask_question(wo["id"],
                     "FORCE_RADIUS_ESCALATE Should the export default to CSV or JSON?")

    daemon._neo_drain()

    central = CentralStore()
    try:
        rows = central.unacked_inbox()
    finally:
        central.close()
    escalations = [i for i in rows if "escalated a question" in i["title"]]
    assert len(escalations) == 1
    item = escalations[0]
    assert "test-forced escalation on the cost of being wrong" in (item["body"] or "")
    haystack = f"{item['title']} {item.get('body') or ''}".lower()
    assert not [s for s in SEATS if s in haystack]

    neo = NeoStore()
    try:
        opinions = {r["seat"] for r in neo.opinions(1)}
    finally:
        neo.close()
    assert opinions == {"premise", "record", "blast", "taste"}, (
        "the deliberation is on file and inspectable — it simply was not pushed — and the "
        "chair is absent because the arbitration, not the chair, made this call"
    )


def test_a_dismissed_gate_reaches_the_worker_through_the_normal_path(
        jarvis_home, fake_claude, tmp_path, project, claude_json):
    """The additive `panel` key, through the delivery path it will actually meet.

    Gate reviews are Neo's highest-volume channel and ~95% of them are classifier false
    positives, so this is the decision the fast path was built for. Every consumer of a
    verdict reads it BY KEY, which is what makes adding one safe — and this is the test
    that would notice if one of them started reading it by shape instead.
    """
    from jarvis import gates

    path = tmp_path / "catalog-panel-gates.json"
    path.write_text(json.dumps({
        "os": {"defaults": {"model": "sonnet"},
               "notifications": {"sinks": ["log"]},
               "neo": {"panel": {"enabled": True}}},
        "projects": [{"name": "proj_a", "path": str(project),
                      "gates": {"enabled": list(gates.KIND_NAMES)}}],
    }))
    ops.start_os(str(path), foreground=True)
    daemon = Daemon(load_catalog(path))
    wo = ops.create_work_order("proj_a", "ship it")
    daemon.tick()
    ops.request_gate_approval(
        wo["id"], "uv run pytest tests/test_release_staging.py -k shipit",
        why="FORCE_ROUTE_FAST FORCE_PROPOSE_DISMISS — this searches for a name",
        evidence="no release is cut")

    daemon._neo_drain()

    calls = [c for c in headless(fake_claude) if seat_of(c)]
    assert [seat_of(c) for c in calls] == ["premise"], "the chair must not have run"
    store_ = ProjectStore(project)
    try:
        approval = store_.list_approvals(wo["id"])[0]
    finally:
        store_.close()
    assert approval["status"] == "dismissed"


def test_a_plan_review_is_not_routed_through_the_panel_by_default(panel_daemon, store,
                                                                  fake_claude):
    """`kinds` defaults to question + approval. A feature order's plan review has its own
    reviewed persona that the seats' mandates say nothing about, so enabling the panel
    must not silently swap one for the other."""
    answer = panel_daemon._panel_answer(panel_daemon.catalog.os.neo)
    q = claim(store, "Release this plan? FORCE_APPROVE", kind="plan")

    result = answer(store, q, "opus", 50)

    assert "panel" not in result
    assert not store.opinions(q["id"])
    assert not [c for c in headless(fake_claude) if seat_of(c)]


def test_with_the_panel_disabled_the_daemon_injects_nothing(started_default_catalog):
    """The seam's off switch, at the one place it is wired: `drain_queue` falls back to
    its own default, so a disabled panel is not a code path at all."""
    daemon = started_default_catalog

    assert daemon._panel_answer(daemon.catalog.os.neo) is None


@pytest.fixture()
def started_default_catalog(jarvis_home, fake_claude, catalog_file, project):
    ops.start_os(str(catalog_file), foreground=True)
    return Daemon(load_catalog(catalog_file))
