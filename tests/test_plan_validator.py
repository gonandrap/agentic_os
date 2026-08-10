"""The plan validator — the backstop that runs whether or not Neo is paying attention.

Neo, not the user, is the routine reviewer of plans, and the design calls the two
backstops that reversal rests on (this and the child cap) load-bearing rather than
nice-to-have. So these tests are written against the four rejections by NAME, not
against whatever the implementation happens to catch: a future simplification that drops
one of them should fail here loudly.

No fixtures, no database, no daemon. `plans.py` is pure functions over a submitted
document on purpose — the checker has to be more trustworthy than the thing it checks.
"""

from __future__ import annotations

import pytest

from jarvis.plans import (
    CHILD_CAP,
    MIN_DESCRIPTION_CHARS,
    PlanError,
    creation_order,
    find_cycles,
    parse_plan,
    render_plan,
)


def child(key: str, needs: list[str] | None = None, description: str | None = None,
          title: str | None = None) -> dict:
    """A child that passes every check, so a test can break exactly one thing."""
    return {
        "key": key,
        "title": title if title is not None else f"Do the {key} piece",
        "description": description if description is not None else (
            f"Implement {key} end to end: change the module that owns it, cover it "
            f"with a test in the existing suite, and leave the public interface "
            f"documented where the neighbouring functions are documented."
        ),
        "needs": needs or [],
    }


def plan(*children: dict, **extra) -> dict:
    return {"children": list(children), **extra}


# -- the shape ------------------------------------------------------------------------


def test_a_well_formed_plan_comes_back_normalised():
    out = parse_plan(plan(child("schema"), child("api", needs=["schema", "schema"]),
                          summary="  add budgets  "))

    assert out["summary"] == "add budgets"
    # Deduplicated, first-seen order, and the optional field is present rather than
    # absent — everything downstream reads the normalised document, never the raw one.
    assert out["children"][1]["needs"] == ["schema"]
    assert out["children"][0]["acceptance"] == ""


def test_a_plan_that_is_not_an_object_or_has_no_children_is_refused():
    with pytest.raises(PlanError, match="must be a JSON object"):
        parse_plan([{"key": "a"}])
    with pytest.raises(PlanError, match="non-empty `children`"):
        parse_plan({"children": []})


def test_every_problem_is_reported_at_once():
    """A planner that has to resubmit per problem burns a session round trip per line."""
    with pytest.raises(PlanError) as e:
        parse_plan(plan(child("a", description="too short"),
                        child("b", title="", needs=["nope"])))

    assert len(e.value.problems) >= 3


# -- rejection 1: cycles ---------------------------------------------------------------


def test_a_dependency_cycle_is_refused_and_names_the_ring():
    """Phase 1's edges are acyclic BY CONSTRUCTION — an edge may only point at a row
    that already exists. A plan is written before any row exists, so that argument does
    not reach it and this is the real cycle check Phase 1 said was owed."""
    with pytest.raises(PlanError) as e:
        parse_plan(plan(child("a", needs=["b"]), child("b", needs=["c"]),
                        child("c", needs=["a"])))

    cycle = [p for p in e.value.problems if "cycle" in p]
    assert cycle and all(k in cycle[0] for k in ("a", "b", "c"))


def test_a_child_that_depends_on_itself_is_refused():
    with pytest.raises(PlanError, match="depends on itself"):
        parse_plan(plan(child("a", needs=["a"])))


def test_two_separate_cycles_are_both_reported():
    cycles = find_cycles([child("a", needs=["b"]), child("b", needs=["a"]),
                          child("c", needs=["d"]), child("d", needs=["c"])])

    assert len(cycles) == 2


def test_a_diamond_is_not_a_cycle():
    """The shape a real decomposition makes constantly: two independent pieces on one
    schema change, joined by a third. A cycle checker that flags this is unusable."""
    parse_plan(plan(child("schema"), child("left", needs=["schema"]),
                    child("right", needs=["schema"]),
                    child("join", needs=["left", "right"])))


# -- rejection 2: unknown ids ----------------------------------------------------------


def test_a_need_naming_nothing_in_the_plan_is_refused():
    """The failure that looks most like success: it would land as a child with no edge."""
    with pytest.raises(PlanError, match="not a child of this plan"):
        parse_plan(plan(child("a"), child("b", needs=["wo-12345678"])))


def test_duplicate_keys_are_refused():
    with pytest.raises(PlanError, match="duplicate key"):
        parse_plan(plan(child("a"), child("a")))


# -- rejection 3: the child cap --------------------------------------------------------


def test_a_plan_at_the_cap_needs_no_justification():
    out = parse_plan(plan(*[child(f"c{i}") for i in range(CHILD_CAP)]))

    assert len(out["children"]) == CHILD_CAP


def test_over_the_cap_without_a_justification_is_refused():
    with pytest.raises(PlanError, match=f"over the cap of {CHILD_CAP}"):
        parse_plan(plan(*[child(f"c{i}") for i in range(CHILD_CAP + 1)]))


def test_over_the_cap_with_a_justification_validates():
    out = parse_plan(plan(*[child(f"c{i}") for i in range(CHILD_CAP + 1)],
                          justification="each migration step must ship separately"))

    assert out["justification"]


# -- rejection 4: descriptions that do not stand alone ---------------------------------


@pytest.mark.parametrize("description", [
    "As discussed in the plan, wire the budget check into the dispatch path and cover it.",
    "Do what the plan above describes for the budget column, then test it thoroughly.",
    "Same as the previous work order, but for the projects table rather than the users.",
    "Implement the second half of the work set out above, with tests alongside it too.",
])
def test_a_description_pointing_outside_itself_is_refused(description):
    """The child worker sees its description and NOTHING else — not the plan, not its
    siblings, not this conversation. A sentence pointing at any of them hands a worker a
    reference it cannot follow."""
    with pytest.raises(PlanError) as e:
        parse_plan(plan(child("a", description=description)))

    assert any("never sees the plan" in p for p in e.value.problems)


@pytest.mark.parametrize("description", [
    # 'As described below' points at the rest of this description, which the worker
    # does get.
    "Add the budget column and the API that reads it, as described below. The column "
    "is nullable, defaults to NULL, and the API omits it entirely when it is unset.",
    # An ordinal is how a good standalone brief is structured, not a reference out of it.
    "The first step is to add the budget column, nullable and defaulting to NULL. The "
    "second is to teach the serializer to omit it when unset. Test both together.",
])
def test_a_description_pointing_INTO_itself_is_fine(description):
    """The negative control for the phrase list, and the half that keeps it honest. A
    checker that rejects good prose trains planners to write worse prose to get past
    it — which costs exactly the context this check exists to protect."""
    parse_plan(plan(child("a", description=description)))


def test_a_description_that_only_restates_the_title_is_refused():
    with pytest.raises(PlanError, match="only repeats the title"):
        parse_plan(plan(child("a", title="Add the budget column",
                              description="Add the budget column")))


def test_a_description_too_short_to_brief_anyone_is_refused():
    with pytest.raises(PlanError, match=f"under the {MIN_DESCRIPTION_CHARS}"):
        parse_plan(plan(child("a", description="Add the column.")))


def test_a_missing_description_says_why_it_matters():
    with pytest.raises(PlanError, match="ONLY thing the child worker will see"):
        parse_plan(plan(child("a", description="")))


# -- creation order --------------------------------------------------------------------


def test_children_are_ordered_so_every_edge_points_backwards():
    """Not tidiness: `create_work_order` refuses an edge pointing at a row that does not
    exist yet, and that refusal is what keeps the live graph acyclic by construction.
    Creating in this order is what lets a validated plan land through it unchanged."""
    out = parse_plan(plan(child("last", needs=["middle"]), child("first"),
                          child("middle", needs=["first"])))

    order = [c["key"] for c in creation_order(out["children"])]

    assert order.index("first") < order.index("middle") < order.index("last")


def test_independent_children_keep_the_order_the_planner_wrote_them_in():
    out = parse_plan(plan(child("b"), child("a"), child("c")))

    assert [c["key"] for c in creation_order(out["children"])] == ["b", "a", "c"]


# -- rendering -------------------------------------------------------------------------


def test_the_rendered_plan_carries_what_a_reviewer_has_to_judge():
    """One renderer, so Neo reviews the same text the user sees when Neo escalates."""
    out = parse_plan(plan(child("schema"), child("api", needs=["schema"]),
                          summary="add budgets"))

    text = "\n".join(render_plan(out))

    assert "add budgets" in text
    assert "needs schema" in text
    assert out["children"][0]["description"] in text
