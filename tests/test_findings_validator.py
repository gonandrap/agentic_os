"""The findings validator — the check that runs before the user is asked to act"""

from __future__ import annotations

import pytest

from jarvis.findings import (
    MAX_EVIDENCE,
    MAX_FIELD_CHARS,
    MAX_FINDINGS,
    MAX_QUOTE_CHARS,
    MAX_SUMMARY_CHARS,
    MIN_FIELD_CHARS,
    MIN_QUOTE_CHARS,
    FindingsError,
    parse_report,
    render_digest,
    render_finding,
    render_report,
    review_headline,
)


def finding(key: str, **overrides) -> dict:
    """A finding that passes every check, so a test can break exactly one thing"""
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
    """A report that passes every check; `extra` knocks out exactly one rule"""
    return {"summary": "Dispatch stalls whenever a row lands mid-tick",
            "findings": list(findings), **extra}


# -- the shape


def test_a_well_formed_report_comes_back_normalised():
    out = parse_report(report(finding("dispatch"), summary="  stalls mid-tick  "))

    assert out["summary"] == "stalls mid-tick"
    # The optional field is PRESENT rather than absent
    assert out["justification"] == ""
    order = out["findings"][0]["proposed_orders"][0]
    assert order["type"] == "work"
    assert order["project"] == "jarvis_os"
    assert set(order) == {"type", "project", "title", "description"}


def test_every_problem_is_reported_at_once():
    """An analyst that has to resubmit per problem burns a session round trip per line of the error it..."""
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
    """The summary is the whole report in one line"""
    with pytest.raises(FindingsError, match="`summary` is required"):
        parse_report(report(finding("a"), summary="  "))


# -- rejection 1: keys


def test_a_missing_or_malformed_key_is_refused():
    with pytest.raises(FindingsError, match="lowercase slug"):
        parse_report(report({**finding("a"), "key": ""}))
    with pytest.raises(FindingsError, match="lowercase slug"):
        parse_report(report({**finding("a"), "key": "Background Jobs!"}))


def test_duplicate_keys_are_refused():
    with pytest.raises(FindingsError, match="duplicate key"):
        parse_report(report(finding("a"), finding("a")))


# -- rejection 2: the findings cap


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


# -- rejection 3: the four prose fields


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
    """The boundary is inclusive: a field may BE the minimum, not merely exceed it"""
    exact = "x" * MIN_FIELD_CHARS
    out = parse_report(report(finding("a", **{field: exact})))

    assert out["findings"][0][field] == exact


@pytest.mark.parametrize("field", sorted(MAX_FIELD_CHARS))
def test_a_prose_field_over_its_cap_is_refused(field):
    """A finding the user has to wade through is one they skim rather than decide"""
    cap = MAX_FIELD_CHARS[field]
    with pytest.raises(FindingsError) as e:
        parse_report(report(finding("a", **{field: "x" * (cap + 1)})))

    assert any(f"`{field}` is {cap + 1} characters, over the {cap}" in p
               for p in e.value.problems)


@pytest.mark.parametrize("field", sorted(MAX_FIELD_CHARS))
def test_a_prose_field_exactly_at_its_cap_is_fine(field):
    exact = "x" * MAX_FIELD_CHARS[field]
    out = parse_report(report(finding("a", **{field: exact})))

    assert out["findings"][0][field] == exact


def test_a_summary_longer_than_one_line_is_refused():
    with pytest.raises(FindingsError, match=f"over the {MAX_SUMMARY_CHARS}"):
        parse_report(report(finding("a"), summary="x" * (MAX_SUMMARY_CHARS + 1)))


# -- rejection 4: evidence


def test_evidence_that_is_missing_empty_or_not_a_list_is_refused():
    with pytest.raises(FindingsError, match="`evidence`"):
        parse_report(report(finding("a", evidence=[])))
    with pytest.raises(FindingsError, match="`evidence`"):
        parse_report(report(finding("a", evidence="jarvis wo show wo-dd8668fa")))


def test_an_evidence_entry_with_no_source_is_refused():
    """A quote nobody can go and check is not evidence"""
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


def test_a_quote_over_its_cap_is_refused():
    """The line that proves the point, not the screen around it"""
    with pytest.raises(FindingsError, match=f"over the {MAX_QUOTE_CHARS}"):
        parse_report(report(finding("a", evidence=[
            {"source": "jarvis wo list", "quote": "x" * (MAX_QUOTE_CHARS + 1)}])))


def test_more_quotes_than_the_cap_is_refused():
    quotes = [{"source": "jarvis wo list", "quote": f"dispatched {n}h ago, no turns"}
              for n in range(MAX_EVIDENCE + 1)]
    with pytest.raises(FindingsError, match=f"over the {MAX_EVIDENCE}"):
        parse_report(report(finding("a", evidence=quotes)))


# -- rejection 5: proposed orders


def test_a_proposed_order_with_no_title_is_refused():
    with pytest.raises(FindingsError, match="`title` is required"):
        parse_report(report(finding("a", proposed_orders=[
            {"type": "work", "title": "", "description": "x" * 200}])))


def test_a_proposed_order_with_a_bad_or_absent_type_is_refused():
    """`type` decides whether the user gets a work order or a whole feature order out of this finding"""
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
    """Reused from `plans._description_problems`"""
    with pytest.raises(FindingsError, match="under the 80"):
        parse_report(report(finding("a", proposed_orders=[
            {"type": "work", "title": "Fix it", "description": "Fix the reconciler."}])))


def test_a_proposed_order_description_pointing_outside_itself_is_refused():
    """Proves the reuse live rather than assuming it: the worker created from this order sees the..."""
    with pytest.raises(FindingsError) as e:
        parse_report(report(finding("a", proposed_orders=[
            {"type": "work", "title": "Fix it",
             "description": "As discussed in the plan, re-derive the reconciler state "
                            "from the work-order table on every tick and cover the "
                            "mid-tick insert with a test in the existing suite."}])))

    assert any("never sees the plan" in p for p in e.value.problems)


def test_a_finding_with_no_proposed_orders_is_accepted():
    """"Do nothing, and here is why" is a legitimate and valuable finding"""
    out = parse_report(report(finding("a", proposed_orders=[])))

    assert out["findings"][0]["proposed_orders"] == []


def test_a_proposed_order_description_opening_with_an_ordinal_is_accepted():
    """The negative control that keeps the inherited phrase list honest: ORDINALS ARE NOT OUTWARD..."""
    out = parse_report(report(finding("a", proposed_orders=[
        {"type": "work", "title": "Re-derive the reconciler state",
         "description": "The first step is to add the query over the work-order table. "
                        "The second is to delete the in-memory seen-set that replaced "
                        "it. Cover the mid-tick insert with a test."}])))

    assert out["findings"][0]["proposed_orders"][0]["description"].startswith("The first")


# -- rendering


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
    """One renderer, so `jarvis io show` and the dashboard page cannot drift"""
    out = parse_report(report(finding("dispatch"), finding("costs"),
                              justification="two mechanisms, not one"))
    out["findings"][0]["status"] = "accepted"

    text = "\n".join(render_report(out))

    assert out["summary"] in text
    assert "two mechanisms, not one" in text
    assert "dispatch" in text and "costs" in text
    assert "accepted" in text


def test_the_review_headline_leads_with_the_counts_and_names_the_command():
    """An attention item whose instruction names no command is one the user has to guess at (§6.2)"""
    out = parse_report(report(finding("a"), finding("b"), finding("c")))
    out["findings"][0]["status"] = "accepted"
    out["findings"][1]["status"] = "rejected"

    line = review_headline({"id": "io-1234abcd", "title": "Why dispatch stalls"}, out)

    assert line.startswith("3 findings on io-1234abcd")
    assert "1 accepted" in line and "1 rejected" in line and "1 awaiting you" in line
    assert "jarvis io review io-1234abcd" in line


def test_the_digest_is_one_line_per_finding_plus_its_work():
    """What `jarvis io show` prints by default: the reader decides from this before opening any finding"""
    long_root = "The reconciler forgets rows. " * 30
    rep = parse_report(report(finding("dispatch", root_cause=long_root[:600]),
                              finding("polling")))

    lines = render_digest(rep)

    assert lines[0] == rep["summary"]
    head = [line for line in lines if line.startswith("- [dispatch] pending: ")]
    assert len(head) == 1 and head[0].endswith("…") and len(head[0]) < 260
    assert any(line.startswith("    do: ") for line in lines)
    assert any("order (work): Re-derive dispatch state" in line for line in lines)
    assert "evidence" not in "\n".join(lines)
