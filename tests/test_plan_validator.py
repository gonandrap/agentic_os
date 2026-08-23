"""The plan validator — the backstop that runs whether or not Neo is paying attention.

Neo, not the user, is the routine reviewer of plans, and the design calls the two
backstops that reversal rests on (this and the child cap) load-bearing rather than
nice-to-have. So these tests are written against the rejections by NAME, not
against whatever the implementation happens to catch: a future simplification that drops
one of them should fail here loudly.

No fixtures, no database, no daemon. `plans.py` is pure functions over a submitted
document on purpose — the checker has to be more trustworthy than the thing it checks.
"""

from __future__ import annotations

import pytest

from jarvis.plans import (
    CHILD_CAP,
    MAX_DESCRIPTION_CHARS,
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
    """A plan that passes every check. `design_doc` is defaulted rather than written
    into each call so a test can knock out exactly the rule it is about — pass
    `design_doc=""` to make it a plan that names no design document."""
    return {"design_doc": "docs/specs/budgets.md", "children": list(children), **extra}


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


# -- rejection 5: descriptions that do not stop ----------------------------------------
# The mirror of rejection 4, and the one the prose could not enforce: `_planner_prompt`
# has said "a brief, not an encyclopedia" for as long as the design-document field has
# existed, and planners still shipped six-kilobyte briefs restating their spec.


def test_a_description_over_the_ceiling_is_refused():
    fat = "Rebuild the exporter. " + ("Context restated from the design document. "
                                      * 40)
    assert len(fat) > MAX_DESCRIPTION_CHARS
    with pytest.raises(PlanError, match="over the"):
        parse_plan(plan(child("schema", description=fat)))


def test_the_ceiling_names_the_way_out():
    """A rejection that does not say what to do instead just gets reworded prose back."""
    fat = "x" * (MAX_DESCRIPTION_CHARS + 1)
    with pytest.raises(PlanError) as e:
        parse_plan(plan(child("schema", description=fat)))
    assert "design document" in str(e.value)
    assert "sections" in str(e.value)


def test_a_description_right_at_the_ceiling_is_fine():
    """The boundary is inclusive: a brief may BE the maximum, not merely approach it."""
    ok = "Ship the schema piece. " + "x" * (MAX_DESCRIPTION_CHARS - 23)
    assert len(ok) == MAX_DESCRIPTION_CHARS
    out = parse_plan(plan(child("schema", description=ok)))
    assert out["children"][0]["description"] == ok


# -- rejection 6: a plan standing on no design document --------------------------------
# The ceiling above is only affordable because a brief may CITE a document instead of
# carrying it. So the document has to exist — or be the first thing the plan builds.


def test_a_plan_with_no_design_document_at_all_is_refused():
    with pytest.raises(PlanError, match="design_doc"):
        parse_plan(plan(child("schema"), design_doc=""))


def test_a_plan_whose_first_child_writes_the_spec_is_accepted():
    out = parse_plan(plan(child("spec"), child("schema", needs=["spec"]),
                          child("api", needs=["schema"]),
                          design_doc="", design_doc_by="spec"))
    assert out["design_doc"] == ""
    assert out["design_doc_by"] == "spec"


def test_the_spec_writing_child_must_be_a_child_of_this_plan():
    with pytest.raises(PlanError, match="not a child of this plan"):
        parse_plan(plan(child("schema"), design_doc="", design_doc_by="ghost"))


def test_a_sibling_that_does_not_wait_for_the_spec_is_refused():
    """A spec written in parallel with the work it governs is a spec nobody can cite."""
    with pytest.raises(PlanError, match="do not depend on it"):
        parse_plan(plan(child("spec"), child("schema"), child("api"),
                        design_doc="", design_doc_by="spec"))


def test_waiting_for_the_spec_through_another_child_counts():
    """The edge may be transitive — `api` needs `schema` needs `spec` is waiting."""
    out = parse_plan(plan(child("spec"), child("schema", needs=["spec"]),
                          child("api", needs=["schema"]),
                          design_doc="", design_doc_by="spec"))
    assert [c["key"] for c in creation_order(out["children"])] == \
        ["spec", "schema", "api"]


def test_naming_both_a_document_and_the_child_that_writes_it_is_refused():
    """`ops.submit_plan` demands a named `design_doc` already exist on disk, so a plan
    claiming both describes two different worlds."""
    with pytest.raises(PlanError, match="not both"):
        parse_plan(plan(child("spec"), child("schema", needs=["spec"]),
                        design_doc="docs/specs/budgets.md", design_doc_by="spec"))


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
