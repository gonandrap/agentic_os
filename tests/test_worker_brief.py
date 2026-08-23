"""The worker briefing: a minimal core plus full sections fetched on demand.

A work order used to open with ~8KB of operating contract, gate briefing and
navigation posture, every worker paying for all of it whether or not the territory
ever came up. The composition is now a compressed core (the load-bearing invariants
only) plus an index of full sections behind `jarvis brief <section>` — the same
pattern the knowledge base already uses: a map plus a retrieval verb, not a payload.

These are the FREE harness checks that CI always runs (the behavioural counterpart —
does a model briefed with the core still ask/finish/gate correctly? — is the opt-in
A/B in evals/llm/test_worker_contract_ab.py):

  * the core stays under its size budget,
  * every `jarvis` command string the core teaches actually parses,
  * every section the index names renders non-empty from the single source,
  * and the full `contract` section still contains everything the pre-split
    contract contained — nothing was lost, only moved.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path

import pytest

from jarvis.catalog import ProjectSpec
from jarvis.gates import GateConfig

WO = {"id": "wo-brief01", "title": "Add exporter",
      "description": "Export the reports."}
SPEC = ProjectSpec(name="p1", path=Path("/tmp/p1"))
GATED = ProjectSpec(name="p1", path=Path("/tmp/p1"),
                    gates=GateConfig(enabled=("release", "pr_merge")))

SECTION_NAMES = ["contract", "gates", "record", "navigation", "knowledge"]


def _prompt(spec: ProjectSpec = SPEC, knowledge=None) -> str:
    from jarvis.dispatch import build_worker_prompt
    return build_worker_prompt(WO, spec, knowledge=knowledge)


# -- the single source ------------------------------------------------------------------

def test_sections_exist_and_render_non_empty():
    from jarvis import worker_brief
    assert worker_brief.section_names() == SECTION_NAMES
    for name in worker_brief.section_names():
        text = worker_brief.render_section(name)
        assert len(text.strip()) > 200, f"section {name!r} renders nearly empty"


def test_unknown_section_raises_naming_the_valid_ones():
    from jarvis import worker_brief
    with pytest.raises(worker_brief.UnknownSection) as e:
        worker_brief.render_section("wat")
    for name in SECTION_NAMES:
        assert name in str(e.value)


def test_sections_render_with_real_ids_on_request():
    from jarvis import worker_brief
    text = worker_brief.render_section("contract", wo_id="wo-abc123",
                                       project="reports_app")
    assert "jarvis wo ask wo-abc123" in text
    assert "reports_app" in text
    assert "<wo-id>" not in text


# -- the core composition ---------------------------------------------------------------

def test_core_contract_is_under_the_budget():
    from jarvis import worker_brief
    p = _prompt()
    core = p[p.index("# Operating contract"):p.index("# Full briefings")]
    assert len(core) < worker_brief.CORE_BUDGET_CHARS, (
        f"core contract is {len(core)} chars — over the "
        f"{worker_brief.CORE_BUDGET_CHARS} budget")
    # The whole bare prompt shrank: it measured 6032 chars before the split.
    assert len(p) < 4500, f"bare worker prompt is {len(p)} chars"


def test_index_names_every_section_and_says_fetching_is_one_command():
    p = _prompt(GATED)
    assert "# Full briefings" in p
    assert "jarvis brief" in p
    for name in SECTION_NAMES:
        assert f"`{name}`" in p, f"index omits section {name!r}"


def test_ungated_project_gets_no_gates_hook():
    """Same rule as the old briefing (and kn-97c41de7): the prompt never points at
    territory the project does not have."""
    p = _prompt()
    assert "`gates`" not in p
    assert "Privileged actions" not in p
    assert "jarvis gate request" not in p


def test_gated_project_core_names_its_live_gates():
    p = _prompt(GATED)
    assert "gated, NOT forbidden" in p
    assert "jarvis gate request" in p
    assert "release" in p


def test_every_core_command_string_parses():
    """The core teaches commands; a typo'd flag would strand a worker mid-session."""
    from jarvis.cli import build_parser
    parser = build_parser()
    p = _prompt(GATED)
    cmds = re.findall(r"`(jarvis [^`\n]+)`", p)
    assert len(cmds) >= 5, "the core stopped teaching its commands inline"
    for cmd in cmds:
        tokens = [re.sub(r"<[^>]*>", "x", t) or "x" for t in shlex.split(cmd)][1:]
        try:
            parser.parse_args(tokens)
        except SystemExit:
            pytest.fail(f"core teaches a command that does not parse: {cmd}")


def test_planner_prompt_is_untouched_by_the_split():
    """The planner keeps its full briefing: it is one session per feature, not the
    fleet's every work order, and its prompt was already reviewed as a unit."""
    from jarvis.dispatch import build_worker_prompt
    planner = build_worker_prompt(
        {"id": "wo-pl1", "title": "t", "description": "d", "kind": "planner",
         "parent_id": "fo-1"}, GATED, [])
    assert "jarvis brief" not in planner
    assert "Serena first, grep second" in planner
    assert "find_referencing_symbols" in planner
    assert "gated, NOT forbidden" in planner


# -- nothing was lost, only moved -------------------------------------------------------

def test_contract_section_contains_everything_the_old_contract_had():
    """The pre-split operating contract, phrase by load-bearing phrase. Each of these
    earned its place (several via LLM evals); the split moves them behind a fetch,
    it does not delete them."""
    from jarvis import worker_brief
    text = worker_brief.render_section("contract", wo_id="wo-brief01", project="p1")
    for phrase in (
        "mirrors the project's OPERATION.md",
        "Work only inside your assigned worktree (you start in it)",
        "Never push to main",
        "The PR title MUST start with `[wo-brief01] `",
        "Neo is your first responder. Any doubt goes to it.",
        "jarvis wo ask wo-brief01",
        "END YOUR TURN",
        "it is not an escalation",
        "does not interrupt the user",
        "one paragraph: the decision, the concrete options, your recommendation",
        'section 3 of design doc "docs/specs/feature.md"',
        "characters are refused",
        "The trigger is DOUBT, not importance",
        "either would work",
        "Ask BEFORE you build on it, not after",
        "Do not talk yourself out of asking",
        "It's reversible",
        "I'll note it as an assumption",
        "REBUILDING",
        "jarvis wo assume wo-brief01",
        "should be RARE",
        "a call you made with NO doubt",
        "Record EVERY such call, including the small and obvious ones",
        "only audit trail",
        # Deferred work reached the backlog by the worker filing it itself until
        # `jarvis wo defer` routed it instead. The DUTY is the load-bearing part and
        # it survived the split; the command under it changed.
        "jarvis wo defer wo-brief01",
        "LOOK IT UP FIRST",
        "jarvis learn search",
        "jarvis learn add",
        "ONLY memory that survives you",
        "jarvis notify --project p1",
        "report-jarvis-bug",
        "jarvis wo finish wo-brief01",
        "--pr <url>",
    ):
        assert phrase in text, f"lost from the contract section: {phrase!r}"


def test_record_section_carries_the_full_record_rule():
    from jarvis import worker_brief
    text = worker_brief.render_section("record", wo_id="wo-brief01")
    for phrase in (
        "The work order record IS this conversation",
        "captured verbatim",
        "neither will ever open this session",
        "jarvis wo finish wo-brief01",
        "--pr <url>",
        "waiting for",
        "ceases to exist",
    ):
        assert phrase in text, f"lost from the record section: {phrase!r}"


def test_navigation_section_is_the_full_navigation_briefing():
    from jarvis import worker_brief
    text = worker_brief.render_section("navigation")
    for phrase in ("Serena first, grep second", "find_referencing_symbols",
                   "If this project has Serena", "no Serena", "read_memory"):
        assert phrase in text, f"lost from the navigation section: {phrase!r}"


def test_gates_section_is_the_full_gate_briefing():
    from jarvis import worker_brief
    text = worker_brief.render_section("gates", wo_id="wo-1",
                                       gates_enabled=("release",))
    for phrase in ("gated, NOT forbidden", "jarvis gate request wo-1", "DISMISSED",
                   "pending or escalated", "second request",
                   "leave the original standing"):
        assert phrase in text, f"lost from the gates section: {phrase!r}"
    assert "pr_merge" not in text, "a gate the project has not enabled is listed"
    # With no project context (bare `jarvis brief gates`) every kind is described,
    # because the section must always render something true.
    everything = worker_brief.render_section("gates")
    for kind in ("pr_merge", "release", "service_restart", "push_protected"):
        assert kind in everything


def test_knowledge_section_teaches_both_halves():
    from jarvis import worker_brief
    text = worker_brief.render_section("knowledge", project="p1")
    assert "jarvis learn search" in text
    assert "jarvis learn show" in text
    assert "jarvis learn add" in text
    assert "LOOK IT UP FIRST" in text


# -- the CLI ----------------------------------------------------------------------------

def _cli(argv, capsys):
    from jarvis.cli import main
    rc = main(argv)
    return rc, capsys.readouterr()


def test_cli_brief_prints_a_section(jarvis_home, capsys):
    rc, out = _cli(["brief", "navigation"], capsys)
    assert rc == 0
    assert "Serena first, grep second" in out.out


def test_cli_brief_renders_real_ids_with_wo(jarvis_home, capsys):
    """--wo substitutes the caller's real id even when the work order cannot be
    located (the command must never fail a worker that holds a valid id)."""
    rc, out = _cli(["brief", "contract", "--wo", "wo-abc12345"], capsys)
    assert rc == 0
    assert "jarvis wo ask wo-abc12345" in out.out


def test_cli_brief_unknown_section_lists_the_valid_ones(jarvis_home, capsys):
    rc, out = _cli(["brief", "wat"], capsys)
    assert rc == 1
    combined = out.out + out.err
    for name in SECTION_NAMES:
        assert name in combined


def test_cli_brief_bare_lists_the_sections(jarvis_home, capsys):
    rc, out = _cli(["brief"], capsys)
    assert rc == 0
    for name in SECTION_NAMES:
        assert name in out.out
