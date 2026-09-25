"""The findings validator — the check that runs before the user is asked to act.

An improvement order ends in a report the user reads and decides on, finding by
finding. The checks below are mechanical for the same reason `plans.py`'s are: a check
on a structured document the user is about to act on has to be more trustworthy than
the thing it checks. So these tests are written against the rejections by NAME, and
every rejection carries a negative control beside it — a checker that refuses good
prose teaches analysts to write worse prose to get past it.

No fixtures, no store, no database, no daemon: `findings.py` is pure functions over a
submitted document.
"""

from __future__ import annotations

import pytest

from jarvis.findings import (
    MAX_FINDINGS,
    MIN_FIELD_CHARS,
    MIN_QUOTE_CHARS,
    FindingsError,
    parse_report,
    render_finding,
    render_report,
    review_headline,
)


def finding(key: str, **overrides) -> dict:
    """A finding that passes every check, so a test can break exactly one thing."""
    base = {
        "key": key,
        "symptom": f"Work orders in the {key} path sit in dispatched for hours with "
                   f"no turn recorded against them.",
        "root_cause": f"The {key} reconciler only re-derives state for rows it has "
                      f"already seen, so a row created mid-tick is never picked up.",
        "why_insufficient": "Restarting the daemon clears the queue and turns the "
                            "dashboard green, which hides the missed rows rather than "
                            "picking them up.",
        "recommendation": "Re-derive from the table on every tick instead of from the "
                          "in-memory set, and cover the mid-tick insert with a test.",
        "evidence": [
            {"source": "jarvis wo show wo-dd8668fa",
             "quote": "dispatched 4h ago, 0 turns recorded"},
        ],
        "proposed_orders": [
            {"type": "work", "project": "jarvis_os",
             "title": f"Re-derive {key} state from the table",
             "description": "Replace the in-memory seen-set in the reconciler with a "
                            "query over the work-order table, so a row inserted while "
                            "a tick is running is picked up by the next one. Cover the "
                            "mid-tick insert with a test in the existing suite."},
        ],
    }
    base.update(overrides)
    return base


def report(*findings: dict, **extra) -> dict:
    """A report that passes every check; `extra` knocks out exactly one rule."""
    return {"summary": "Dispatch stalls whenever a row lands mid-tick",
            "findings": list(findings), **extra}


# -- the shape ------------------------------------------------------------------------


def test_a_well_formed_report_comes_back_normalised():
    out = parse_report(report(finding("dispatch"), summary="  stalls mid-tick  "))

    assert out["summary"] == "stalls mid-tick"
    # The optional field is PRESENT rather than absent — everything downstream reads
    # the normalised document, never the raw one.
    assert out["justification"] == ""
    order = out["findings"][0]["proposed_orders"][0]
    assert order["type"] == "work"
    assert order["project"] == "jarvis_os"
    assert set(order) == {"type", "project", "title", "description"}


def test_every_problem_is_reported_at_once():
    """An analyst that has to resubmit per problem burns a session round trip per line
    of the error it could have had at once."""
    with pytest.raises(FindingsError) as e:
        parse_report(report(
            finding("a", symptom="", why_insufficient="too short"),
            finding("b", evidence=[]),
        ))

    problems = e.value.problems
    assert any("`symptom` is required" in p for p in problems)
    assert any("`why_insufficient`" in p and "characters" in p for p in problems)
    assert any("`evidence`" in p for p in problems)
    assert len(problems) >= 3


def test_a_report_that_is_not_an_object_or_has_no_findings_is_refused():
    with pytest.raises(FindingsError, match="must be a JSON object"):
        parse_report([{"key": "a"}])
    with pytest.raises(FindingsError, match="non-empty `findings`"):
        parse_report(report())


def test_a_report_with_no_summary_is_refused():
    """The summary is the whole report in one line; without it the attention item and
    the dashboard page open on a finding list with no subject."""
    with pytest.raises(FindingsError, match="`summary` is required"):
        parse_report(report(finding("a"), summary="  "))


# -- rejection 1: keys -----------------------------------------------------------------
# `key` is how `jarvis io review` and the dashboard address one finding, so a missing,
# malformed or duplicated one makes a decision unaddressable.


def test_a_missing_or_malformed_key_is_refused():
    with pytest.raises(FindingsError, match="lowercase slug"):
        parse_report(report({**finding("a"), "key": ""}))
    with pytest.raises(FindingsError, match="lowercase slug"):
        parse_report(report({**finding("a"), "key": "Background Jobs!"}))


def test_duplicate_keys_are_refused():
    with pytest.raises(FindingsError, match="duplicate key"):
        parse_report(report(finding("a"), finding("a")))


# -- rejection 2: the findings cap -----------------------------------------------------
# The ATTENTION cap, same argument as `plans.CHILD_CAP`: a report the user will not read
# changes nothing.


def test_a_report_at_the_cap_needs_no_justification():
    out = parse_report(report(*[finding(f"f{i}") for i in range(MAX_FINDINGS)]))

    assert len(out["findings"]) == MAX_FINDINGS


def test_over_the_cap_without_a_justification_is_refused():
    with pytest.raises(FindingsError, match=f"over the cap of {MAX_FINDINGS}"):
        parse_report(report(*[finding(f"f{i}") for i in range(MAX_FINDINGS + 1)]))


def test_over_the_cap_with_a_justification_validates():
    out = parse_report(report(*[finding(f"f{i}") for i in range(MAX_FINDINGS + 1)],
                              justification="each stall has a different mechanism"))

    assert out["justification"]


# -- rejection 3: the four prose fields ------------------------------------------------
# Short prose is the failure mode for `why_insufficient` specifically: "it does not fix
# the root cause" is a restatement, not an argument.


@pytest.mark.parametrize("field",
                         ["symptom", "root_cause", "why_insufficient", "recommendation"])
def test_a_missing_prose_field_is_refused(field):
    with pytest.raises(FindingsError, match=f"`{field}` is required"):
        parse_report(report(finding("a", **{field: "  "})))


@pytest.mark.parametrize("field",
                         ["symptom", "root_cause", "why_insufficient", "recommendation"])
def test_a_prose_field_under_the_floor_is_refused(field):
    short = "x" * (MIN_FIELD_CHARS - 1)
    with pytest.raises(FindingsError) as e:
        parse_report(report(finding("a", **{field: short})))

    assert any(f"`{field}` is {MIN_FIELD_CHARS - 1} characters" in p
               and str(MIN_FIELD_CHARS) in p for p in e.value.problems)


@pytest.mark.parametrize("field",
                         ["symptom", "root_cause", "why_insufficient", "recommendation"])
def test_a_prose_field_exactly_at_the_floor_is_fine(field):
    """The boundary is inclusive: a field may BE the minimum, not merely exceed it."""
    exact = "x" * MIN_FIELD_CHARS
    out = parse_report(report(finding("a", **{field: exact})))

    assert out["findings"][0][field] == exact


# -- rejection 4: evidence -------------------------------------------------------------
# A finding with no quoted evidence is an opinion, and the user is being asked to spend
# worker sessions on it.


def test_evidence_that_is_missing_empty_or_not_a_list_is_refused():
    with pytest.raises(FindingsError, match="`evidence`"):
        parse_report(report(finding("a", evidence=[])))
    with pytest.raises(FindingsError, match="`evidence`"):
        parse_report(report(finding("a", evidence="jarvis wo show wo-dd8668fa")))


def test_an_evidence_entry_with_no_source_is_refused():
    """A quote nobody can go and check is not evidence; the source is how the user
    verifies it without reading the analyst's session."""
    with pytest.raises(FindingsError) as e:
        parse_report(report(finding("a", evidence=[
            {"quote": "dispatched 4h ago, 0 turns recorded"}])))

    assert any("`source` is required" in p and "'a'" in p and "1" in p
               for p in e.value.problems)


def test_an_evidence_entry_with_no_quote_is_refused():
    with pytest.raises(FindingsError, match="`quote` is required"):
        parse_report(report(finding("a", evidence=[{"source": "jarvis wo list"}])))


def test_an_evidence_entry_that_is_not_an_object_is_refused():
    with pytest.raises(FindingsError, match="must be an object"):
        parse_report(report(finding("a", evidence=["dispatched 4h ago"])))


def test_a_quote_under_the_floor_is_refused():
    with pytest.raises(FindingsError, match=f"under the {MIN_QUOTE_CHARS}"):
        parse_report(report(finding("a", evidence=[
            {"source": "jarvis wo list", "quote": "stalled"}])))


def test_a_quote_exactly_at_the_floor_is_fine():
    exact = "x" * MIN_QUOTE_CHARS
    out = parse_report(report(finding("a", evidence=[
        {"source": "jarvis wo list", "quote": exact}])))

    assert out["findings"][0]["evidence"][0]["quote"] == exact


# -- rejection 5: proposed orders ------------------------------------------------------


def test_a_proposed_order_with_no_title_is_refused():
    with pytest.raises(FindingsError, match="`title` is required"):
        parse_report(report(finding("a", proposed_orders=[
            {"type": "work", "title": "", "description": "x" * 200}])))


def test_a_proposed_order_with_a_bad_or_absent_type_is_refused():
    """`type` decides whether the user gets a work order or a whole feature order out of
    this finding, so defaulting it would pick that on the analyst's behalf."""
    with pytest.raises(FindingsError, match="work, feature"):
        parse_report(report(finding("a", proposed_orders=[
            {"type": "epic", "title": "Fix it", "description": "x" * 200}])))
    with pytest.raises(FindingsError, match="work, feature"):
        parse_report(report(finding("a", proposed_orders=[
            {"title": "Fix it", "description": "x" * 200}])))


def test_a_proposed_order_with_a_non_string_project_is_refused():
    with pytest.raises(FindingsError, match="`project` must be a string"):
        parse_report(report(finding("a", proposed_orders=[
            {"type": "work", "project": ["jarvis_os"], "title": "Fix it",
             "description": "x" * 200}])))


def test_a_proposed_order_that_is_not_an_object_is_refused():
    with pytest.raises(FindingsError, match="must be an object"):
        parse_report(report(finding("a", proposed_orders=["Fix the reconciler"])))


def test_proposed_orders_that_is_not_a_list_is_refused():
    with pytest.raises(FindingsError, match="`proposed_orders` must be a list"):
        parse_report(report(finding("a", proposed_orders={"title": "Fix it"})))


def test_a_proposed_order_description_too_short_to_brief_anyone_is_refused():
    """Reused from `plans._description_problems`, not reimplemented: this description
    becomes a real brief read cold by a stranger, and there is one standard for that."""
    with pytest.raises(FindingsError, match="under the 80"):
        parse_report(report(finding("a", proposed_orders=[
            {"type": "work", "title": "Fix it", "description": "Fix the reconciler."}])))


def test_a_proposed_order_description_pointing_outside_itself_is_refused():
    """Proves the reuse live rather than assuming it: the worker created from this order
    sees the description and never the findings report it came out of."""
    with pytest.raises(FindingsError) as e:
        parse_report(report(finding("a", proposed_orders=[
            {"type": "work", "title": "Fix it",
             "description": "As discussed in the plan, re-derive the reconciler state "
                            "from the work-order table on every tick and cover the "
                            "mid-tick insert with a test in the existing suite."}])))

    assert any("never sees the plan" in p for p in e.value.problems)


def test_a_finding_with_no_proposed_orders_is_accepted():
    """"Do nothing, and here is why" is a legitimate and valuable finding. Forcing an
    order out of it is how an analyst is pushed into inventing work."""
    out = parse_report(report(finding("a", proposed_orders=[])))

    assert out["findings"][0]["proposed_orders"] == []


def test_a_proposed_order_description_opening_with_an_ordinal_is_accepted():
    """The negative control that keeps the inherited phrase list honest: ORDINALS ARE
    NOT OUTWARD REFERENCES. "The first step is…" is how a good standalone brief opens,
    and rejecting it would teach analysts to write worse prose to get past the checker.
    Mirrors the same control in tests/test_plan_validator.py."""
    out = parse_report(report(finding("a", proposed_orders=[
        {"type": "work", "title": "Re-derive the reconciler state",
         "description": "The first step is to add the query over the work-order table. "
                        "The second is to delete the in-memory seen-set that replaced "
                        "it. Cover the mid-tick insert with a test."}])))

    assert out["findings"][0]["proposed_orders"][0]["description"].startswith("The first")


# -- rendering -------------------------------------------------------------------------


def test_the_rendered_finding_carries_what_a_reviewer_has_to_judge():
    out = parse_report(report(finding("dispatch")))
    f = out["findings"][0]

    text = "\n".join(render_finding(f))

    for field in ("symptom", "root_cause", "why_insufficient", "recommendation"):
        assert f[field] in text
    assert f["evidence"][0]["quote"] in text
    assert f["evidence"][0]["source"] in text
    assert f["proposed_orders"][0]["title"] in text


def test_a_finding_with_no_proposed_orders_says_so():
    out = parse_report(report(finding("a", proposed_orders=[])))

    text = "\n".join(render_finding(out["findings"][0]))

    assert "no proposed orders" in text.lower()


def test_the_rendered_report_carries_the_summary_the_keys_and_the_decisions():
    """One renderer, so `jarvis io show` and the dashboard page cannot drift."""
    out = parse_report(report(finding("dispatch"), finding("costs"),
                              justification="two mechanisms, not one"))
    out["findings"][0]["status"] = "accepted"

    text = "\n".join(render_report(out))

    assert out["summary"] in text
    assert "two mechanisms, not one" in text
    assert "dispatch" in text and "costs" in text
    assert "accepted" in text


def test_the_review_headline_leads_with_the_counts_and_names_the_command():
    """An attention item whose instruction names no command is one the user has to guess
    at (§6.2)."""
    out = parse_report(report(finding("a"), finding("b"), finding("c")))
    out["findings"][0]["status"] = "accepted"
    out["findings"][1]["status"] = "rejected"

    line = review_headline({"id": "io-1234abcd", "title": "Why dispatch stalls"}, out)

    assert line.startswith("3 findings on io-1234abcd")
    assert "1 accepted" in line and "1 rejected" in line and "1 awaiting you" in line
    assert "jarvis io review io-1234abcd" in line
