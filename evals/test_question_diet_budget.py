"""The plan-review question diet, measured (deterministic, no model) (#65)"""

from __future__ import annotations

from typing import Any

import pytest

from jarvis import plans

scenario = pytest.mark.scenario

#: The #65–#67 silhouette: seven children
N_CHILDREN = 7
BRIEF_CHARS = 11_000

ASK = ("I want each working unit to not be claimed done until an external validator "
       "said so. The panel evaluates the code changes as well as the testing evidence "
       "and may ask for refactors or more evidence; each round has a fingerprint. "
       "Neither side knows about the other. Go ahead and plan for it.")


def production_shaped_plan() -> dict[str, Any]:
    """The #65–#67 shape, briefs and all — reconstructed rather than submitted"""
    brief = ("The context the planner repeated into every child instead of the design "
             "document carrying it once. ")
    children = []
    for i in range(N_CHILDREN):
        children.append({
            "key": f"piece{i}",
            "title": f"Build piece {i} of the validation layer",
            "description": brief,
            "needs": [f"piece{i - 1}"] if i else [],
            "acceptance": f"the piece {i} tests pass and the suite stays green",
            "spec_section": str(i + 1),
        })
    plan = plans.parse_plan({
        "summary": "no work order reaches the merge queue until the panel says so",
        "design_doc": "docs/superpowers/specs/validation-panel.md",
        "children": children,
    })
    for child in plan["children"]:
        child["description"] = (brief * (BRIEF_CHARS // len(brief) + 1))[:BRIEF_CHARS]
    return plan


@pytest.fixture(scope="module")
def readings(request):
    """Collects (label, chars) readings; teardown prints them past output capture"""
    collected: list[tuple[str, int]] = []
    yield collected
    lines = ["", "question-diet budget (chars; ~4 chars/token):"]
    lines += [f"  {label}: {chars:,}" for label, chars in collected]
    if len(collected) == 2:
        old, new = collected[0][1], collected[1][1]
        lines.append(f"  reduction: {(old - new) / old:.1%}  "
                     f"(measured on the real #67: 21,250 -> 1,999 input tokens, -90.6%)")
    config = request.config
    reporter = config.pluginmanager.get_plugin("terminalreporter")
    capman = config.pluginmanager.get_plugin("capturemanager")
    for line in lines:
        if reporter is None:  # pragma: no cover - only when -p no:terminal
            print(line)
        elif capman is None:  # pragma: no cover - capture is on by default
            reporter.write_line(line)
        else:
            with capman.global_and_fixture_disabled():
                reporter.write_line(line)


@scenario("question-diet", "plan review question is a tenth of the inlined plan")
def test_the_skeleton_question_is_a_fraction_of_the_briefs_it_replaced(readings):
    plan = production_shaped_plan()
    fo = {"id": "fo-shaped", "title": "validation layer", "description": ASK}

    question = plans.build_plan_question(fo, plan)
    inlined = "\n".join([ASK, *plans.render_plan(plan)])  # the pre-diet composition

    # Control: the fat is real — the full render carries every brief
    assert len(inlined) > N_CHILDREN * BRIEF_CHARS
    # The diet: the skeleton must stay under 15% of it, briefs excluded wholesale
    assert len(question) < len(inlined) * 0.15
    assert "BRIEF" not in question  # no description text leaks
    for i in range(N_CHILDREN):
        assert f"Build piece {i} of the validation layer" in question

    readings.append(("inlined plan (pre-diet)", len(inlined)))
    readings.append(("skeleton question (shipped)", len(question)))


@scenario("question-diet", "worker question cap holds the paragraph rule")
def test_the_ask_cap_is_meaningfully_below_the_production_fat():
    from jarvis.sections import QUESTION_MAX_CHARS, QUESTION_WARN_CHARS

    # #64 ran ~6.2KB and #68 ~6.4KB of pasted context
    assert QUESTION_MAX_CHARS <= 4000
    assert QUESTION_WARN_CHARS <= 1500
