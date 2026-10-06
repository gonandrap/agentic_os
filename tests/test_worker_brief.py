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

SECTION_NAMES = ["contract", "gates", "record", "navigation", "concision",
                 "knowledge"]


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
    # The whole bare prompt shrank: it measured 6032 chars before the split. 4500 until
    # the crew block (spec 2026-09-23-the-crew-a-worker-must-use.md SS6) bought its place.
    # Raised 5000 -> 5900 for the inlined navigation block (spec
    # 2026-10-01-the-steer-that-beat-the-brief.md SS3): 4911 before, 5846 after. 935 of
    # that, not the ~500 the spec estimated, because SELECT_LINE names BOTH tool prefixes
    # in one select — 325 chars of tool names, and the probe that made one line correct is
    # what bought back the retry sentence. The CORE budget above is untouched: this block
    # is after the index, which is the whole reason it goes there.
    assert len(p) < 5900, f"bare worker prompt is {len(p)} chars"


def test_the_core_says_to_pass_a_reference_and_never_a_payload():
    """§5 of docs/superpowers/specs/2026-09-26-bounded-model-inputs.md, and it earns the
    core rather than the fetched section for the budget comment's test: the damage — a
    `$(git diff)` expanded into this worker's context and the target worker's next turn —
    is done on the FIRST send, before any section could be fetched. It was paid for by
    cutting, not by raising the budget."""
    p = _prompt()

    assert "REFERENCE" in p
    for alternative in ("pull request URL", "commit SHA", "line range",
                        "command that reproduces it"):
        assert alternative in p, f"the core does not offer {alternative!r}"
    assert "refused before the command runs" in p
    # The rule is stated in the fetched section too, for the worker that goes looking.
    from jarvis import worker_brief
    assert "REFERENCE" in worker_brief.render_section("concision")


def test_the_evidence_re_finish_rule_survived_the_cut_into_the_record_section():
    """The sentence the core gave up to pay for the rule above is not allowed to vanish:
    a worker whose review comes back asking for more has to know that finishing again
    with the fuller account is the move."""
    from jarvis import worker_brief
    assert "finish again" in worker_brief.render_section("record")


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


def test_planner_brief_requires_the_spec_to_be_committed():
    """ops.submit_plan reads the spec with `git show`, never from the working tree
    (docs/superpowers/specs/2026-09-25-plan-review-reads-the-spec-the-os-holds.md
    sections 6 and 11). A planner told only that the file "must already exist" writes
    it, leaves it uncommitted and hits that refusal with no warning."""
    import json
    from jarvis.dispatch import build_worker_prompt
    planner = build_worker_prompt(
        {"id": "wo-pl2", "title": "t", "description": "d", "kind": "planner",
         "parent_id": "fo-1"}, GATED, [])
    assert "must already be COMMITTED" in planner
    assert 'must already exist"' not in planner
    assert "not on disk" not in planner
    assert "It must exist before you submit" not in planner
    assert "not committed" in planner
    block = re.search(r"```json\n(.*?)```", planner, re.S).group(1)
    json.loads(block)


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
    """`ceases to exist` was here until 2026-09-19 and is deliberately gone.

    It was half of a contradiction: the same section called `--summary` "a one-line
    headline" and warned that a detail living only there ceased to exist, which is
    answerable only by writing the detail in both places — and the record shows that is
    what workers did (spec 2026-09-19 SS7). The rule it protected survives in the
    `never a substitute` clause below; what is gone is the licence to pad the summary.
    """
    from jarvis import worker_brief
    text = worker_brief.render_section("record", wo_id="wo-brief01")
    for phrase in (
        "The work order record IS this conversation",
        "captured verbatim",
        "neither will ever open this session",
        "jarvis wo finish wo-brief01",
        "--pr <url>",
        "waiting for",
        "never a substitute for the final message",
    ):
        assert phrase in text, f"lost from the record section: {phrase!r}"
    assert "ceases to exist" not in text, (
        "the summary/final-message contradiction is back — see spec 2026-09-19 SS7")


def test_navigation_section_is_the_full_navigation_briefing():
    from jarvis import worker_brief
    text = worker_brief.render_section("navigation")
    for phrase in ("Serena first, grep second", "find_referencing_symbols",
                   "DEFERRED", "read_memory"):
        assert phrase in text, f"lost from the navigation section: {phrase!r}"
    # §3 item 3 of docs/superpowers/specs/2026-10-01-the-steer-that-beat-the-brief.md:
    # the symbol tools are DEFERRED behind `ToolSearch`, so "appear in your tool list"
    # reads FALSE at the moment the worker reads it — a test it fails by design.
    assert "appear in your tool list" not in text
    assert "If this project has Serena" not in text


# -- the inlined navigation block (spec 2026-10-01-the-steer-that-beat-the-brief.md §3) --

#: The recovery call, asserted as a literal because it is the one call the block is
#: allowed to cost and a typo in a tool name spends it for nothing. Both prefixes in one
#: select: `mcp__serena__` from `claude mcp add serena`, `mcp__plugin_serena_serena__`
#: from a plugin install, and an absent name is silently ignored — probed on 2.1.284,
#: where a select naming both spellings of `find_symbol` on a plugin install returned
#: only `mcp__plugin_serena_serena__find_symbol` and the rest of the select survived.
SELECT_LINE = (
    "select:mcp__serena__find_symbol,mcp__plugin_serena_serena__find_symbol,"
    "mcp__serena__find_referencing_symbols,"
    "mcp__plugin_serena_serena__find_referencing_symbols,"
    "mcp__serena__get_symbols_overview,"
    "mcp__plugin_serena_serena__get_symbols_overview,"
    "mcp__serena__activate_project,mcp__plugin_serena_serena__activate_project")


def test_the_navigation_block_is_inlined_after_the_section_index():
    """Cause 3 of the defect: an ordinary lead is never told the posture at all — it gets
    one index line pointing at `jarvis brief navigation`, and the measured fetch
    behaviour is that it does not fetch. So the block is INLINE.

    After the index, not inside `# Operating contract`: the core has 38 chars of headroom
    against CORE_BUDGET_CHARS, and every sentence in it is A/B-graded as a unit by
    evals/llm/test_worker_contract_ab.py (§3, rejected alternative 7)."""
    p = _prompt()
    assert SELECT_LINE in p, "the ToolSearch recovery call is not in the bare prompt"
    assert p.index("# Full briefings on demand") < p.index(SELECT_LINE)


def test_the_navigation_block_says_the_tools_are_deferred_not_absent():
    p = _prompt()
    assert "DEFERRED" in p
    assert "appear in your tool list" not in p


def test_the_navigation_block_overrides_the_bash_first_steer():
    """Belt and braces on §1: it survives a project setting `relaxed` or `cli`, and it
    survives the per-session cohort draw if a future CLI re-introduces the steer under a
    different name."""
    assert "does NOT govern code navigation" in _prompt()


def test_the_navigation_block_ranks_the_calls_by_what_grep_cannot_do():
    p = _prompt()
    for call in ("find_referencing_symbols", "get_symbols_overview", "find_symbol",
                 "activate_project"):
        assert call in p, f"the navigation block never names {call}"


def test_the_navigation_block_is_absent_when_serena_is_deselected():
    """`section_index` already swaps in NO_SERENA_HOOK and the fetched section already
    carries the grep posture, so a block recommending a server this same dispatch removed
    would be the incoherence `wiring.serena_wired` exists to prevent."""
    from jarvis.catalog import WiringConfig
    unwired = ProjectSpec(
        name="p1", path=Path("/tmp/p1"),
        wiring=WiringConfig(disabled_plugins=("serena@claude-plugins-official",)))
    p = _prompt(unwired)
    assert SELECT_LINE not in p
    assert "mcp__serena__" not in p
    assert "DEFERRED" not in p


def test_gates_section_is_the_full_gate_briefing():
    from jarvis import worker_brief
    text = worker_brief.render_section("gates", wo_id="wo-1",
                                       gates_enabled=("release",))
    for phrase in ("gated, NOT forbidden", "jarvis gate request wo-1", "DISMISSAL",
                   "jarvis gate contest <request-number>", "jarvis gate explain",
                   "pending or escalated", "second request",
                   "leave the original standing"):
        assert phrase in text, f"lost from the gates section: {phrase!r}"
    # The ask belongs to the KIND, and only the live kinds' asks are shown — the brief is
    # the surface a worker reads before it is ever blocked (spec 2026-09-12 §6).
    assert "why this is ready to ship" in text
    assert "why this service has to be interrupted" not in text
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


# -- the crew (spec 2026-09-23-the-crew-a-worker-must-use.md SS6) -------------------------

def test_worker_core_names_both_crew_seats():
    p = _prompt()
    assert "jarvis-spec-writer" in p and "jarvis-implementer" in p
    assert "# Your crew" in p


def test_worker_core_keeps_git_the_pr_and_the_record_with_the_lead():
    from jarvis import worker_brief
    core = "\n".join(worker_brief.core_contract("wo-crew01", "t", "p1",
                                                has_knowledge=False))
    for kept in ("git", "pr", "`jarvis", "review"):
        assert kept in core.lower()
    # the lead's own editing is refused by a hook, so the block states the mechanism
    assert "refused" in core.lower()


def test_planner_prompt_names_no_crew_through_the_production_path():
    """Not `core_contract(kind="planner")` — the real briefing a planner is dispatched
    with. A planner never reaches the worker branch at all, so a unit test on the
    argument would pass even if every planner were told to delegate to a seat it was
    never handed."""
    from jarvis.dispatch import build_worker_prompt
    planner = dict(WO, id="wo-crew02", kind="planner")
    out = build_worker_prompt(planner, SPEC)
    assert "# Your crew" not in out
    assert "jarvis-spec-writer" not in out and "jarvis-implementer" not in out


def test_manager_prompt_names_no_crew_through_the_production_path():
    from jarvis.dispatch import build_worker_prompt
    manager = dict(WO, id="wo-crew03", kind="manager")
    out = build_worker_prompt(manager, SPEC)
    assert "# Your crew" not in out
    assert "jarvis-spec-writer" not in out and "jarvis-implementer" not in out


def test_the_same_path_with_kind_worker_does_carry_the_crew():
    """The other half: the assertions above must fail for the right reason, not
    because `build_worker_prompt` never emits the block for anyone."""
    from jarvis.dispatch import build_worker_prompt
    for kind in (None, "worker"):
        wo = dict(WO, id="wo-crew04")
        if kind:
            wo["kind"] = kind
        out = build_worker_prompt(wo, SPEC)
        assert "# Your crew" in out
        assert "jarvis-spec-writer" in out and "jarvis-implementer" in out


def test_build_worker_prompt_passes_the_kind_down():
    """`core_contract` takes `kind`; a production caller has to hand it over or the
    default silently decides for every work order."""
    import inspect

    from jarvis import dispatch
    source = inspect.getsource(dispatch.build_worker_prompt)
    assert "kind=str(wo.get(\"kind\")" in source


def test_template_version_bumped_for_crew():
    from jarvis.bootstrap import TEMPLATE_VERSION
    assert TEMPLATE_VERSION >= 12


# -- §6 of 2026-09-29-a-lead-must-not-block-past-its-cache.md: the poll rhythm ----------


def test_core_contract_carries_the_poll_rhythm():
    """The bullet asked for the behaviour that cost $52 (issue 868). What replaces the
    prescription is the rhythm, with the numbers a lead has to act on."""
    from jarvis import worker_brief

    core = "\n".join(worker_brief.core_contract("wo-brief01", "t", "p1",
                                               has_knowledge=False))
    bullet = [line for line in core.split("\n")
              if line.startswith("- **A turn is one-shot")]

    assert len(bullet) == 1
    text = bullet[0]
    assert "BACKGROUND" in text and "run_in_background" in text
    assert "3-4 minutes" in text
    assert "240 seconds" in text
    assert "uncollected" in text
    # …and the heading sentence stays: it is still true, and it is WHY the rhythm is a
    # poll and not a wait.
    assert "NOTHING wakes you" in text
    # Everything else still may not be backgrounded.
    assert "REFUSED" in text


def test_core_contract_no_longer_says_re_run_in_the_foreground():
    """The defect, stated as policy. `hooks.long_foreground_decision` now refuses it."""
    from jarvis import worker_brief

    core = "\n".join(worker_brief.core_contract("wo-brief01", "t", "p1",
                                                has_knowledge=False))

    assert "re-run it in the FOREGROUND and wait" not in core
    assert "in the FOREGROUND and wait" not in core


def test_standing_instructions_carry_the_carve_out():
    """The same rewrite at prose length, and the three things that start the next turn
    stay: nothing about backgrounding changes the fact that no event re-invokes a
    worker."""
    from jarvis import worker_brief

    section = worker_brief.render_section("record", wo_id="wo-brief01", project="p1")

    assert "So work that outlasts one command runs in the FOREGROUND" not in section
    assert "run_in_background" in section
    assert "3-4 minutes" in section
    assert "240 seconds" in section
    assert "1.25x" in section and "0.1x" in section
    for resumer in ("jarvis wo ask", "gate", "message"):
        assert resumer in section
    assert "I'll pick this up when the" in section


def test_template_version_bumped():
    """Prose is the whole mechanism for an already-bootstrapped repo, and it only
    reaches one through the bump."""
    from jarvis.bootstrap import TEMPLATE_VERSION

    assert TEMPLATE_VERSION >= 13
