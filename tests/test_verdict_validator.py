"""The verdict validator — §2.4 and §5.4 of
docs/superpowers/specs/2026-09-27-investigation-orders.md.

Held to `plans.py`'s standard: every rejection has a negative control beside it, and
every problem is reported at once so one revision fixes all of them. Pure Python over a
submitted document — no store, no OS, no fixtures.
"""

from __future__ import annotations

import json

import pytest

from jarvis import findings, gaps, verdicts
from jarvis.testing import a_verdict


def test_the_four_classifications_are_the_whole_vocabulary():
    assert verdicts.CLASSIFICATIONS == ("GAP", "WAITING_ON_USER", "TRANSIENT",
                                        "ALREADY_TRACKED")


def test_each_classification_is_accepted_with_its_own_payload():
    for classification in verdicts.CLASSIFICATIONS:
        doc = verdicts.parse_verdict(a_verdict(classification))
        assert doc["classification"] == classification
        assert doc["subject"] == "wo-11111111"
        assert doc["evidence"][0]["source"].startswith("jarvis ")


def test_a_payload_that_does_not_match_its_classification_is_refused():
    gap = a_verdict("GAP")
    gap.pop("proposed_fix")
    with pytest.raises(verdicts.VerdictError) as e:
        verdicts.parse_verdict(gap)
    assert any("proposed_fix" in p for p in e.value.problems)

    transient = a_verdict("TRANSIENT", user_owes="assumption as-1 on wo-11111111")
    with pytest.raises(verdicts.VerdictError) as e:
        verdicts.parse_verdict(transient)
    assert any("user_owes" in p for p in e.value.problems)

    # The control: the same two documents untouched.
    assert verdicts.parse_verdict(a_verdict("GAP"))["proposed_fix"]["title"]
    assert verdicts.parse_verdict(a_verdict("TRANSIENT"))["unsticks"]["what"]


def test_a_gap_whose_proposed_fix_could_not_be_filed_is_refused():
    """`bugreport.report_bug` refuses without `expected`/`actual`, and refusing here
    costs one revision instead of a settled order with nothing filed (§2.5)."""
    fix = dict(a_verdict("GAP")["proposed_fix"])
    fix.pop("expected")
    with pytest.raises(verdicts.VerdictError) as e:
        verdicts.parse_verdict(a_verdict("GAP", proposed_fix=fix))
    assert any("expected" in p for p in e.value.problems)


def test_a_paraphrased_quote_is_refused_by_the_shared_floor():
    short = [{"source": "jarvis wo show wo-11111111", "quote": "it is stuck"}]
    with pytest.raises(verdicts.VerdictError) as e:
        verdicts.parse_verdict(a_verdict("TRANSIENT", evidence=short))
    assert any(str(findings.MIN_QUOTE_CHARS) in p for p in e.value.problems)
    assert verdicts.MIN_QUOTE_CHARS == findings.MIN_QUOTE_CHARS


def test_evidence_is_one_definition_shared_with_findings():
    """§2.4: `findings.parse_evidence` is public and `verdicts` imports it rather than
    growing a second answer to what a quote is."""
    problems: list[str] = []
    assert findings.parse_evidence("k", [{"source": "s", "quote": "x" * 40}], problems)
    assert not problems


def test_a_verdict_about_something_else_is_not_a_verdict_about_this_order():
    with pytest.raises(verdicts.VerdictError) as e:
        verdicts.parse_verdict(a_verdict("TRANSIENT"), subject="wo-99999999")
    assert any("wo-99999999" in p for p in e.value.problems)
    # Control: the recorded subject matches.
    assert verdicts.parse_verdict(a_verdict("TRANSIENT"), subject="wo-11111111")


def test_an_unknown_classification_names_the_four():
    with pytest.raises(verdicts.VerdictError) as e:
        verdicts.parse_verdict(a_verdict("STUCK"))
    assert any("GAP" in p and "TRANSIENT" in p for p in e.value.problems)


def test_a_document_over_the_cap_is_refused_with_the_fix_named():
    fat = a_verdict("TRANSIENT")
    fat["root_cause"] = "x" * (verdicts.MAX_VERDICT_CHARS + 10)
    with pytest.raises(verdicts.VerdictError) as e:
        verdicts.parse_verdict(fat)
    assert any(str(verdicts.MAX_VERDICT_CHARS) in p for p in e.value.problems)
    assert len(json.dumps(a_verdict("TRANSIENT"))) < verdicts.MAX_VERDICT_CHARS


def test_duplicate_of_must_name_something_resolvable():
    for ref in ("#790", "https://github.com/x/y/issues/790", "wo-4beada49",
                "io-1234abcd", "inv-1234abcd"):
        assert verdicts.parse_verdict(
            a_verdict("ALREADY_TRACKED", duplicate_of=ref))["duplicate_of"] == ref
    with pytest.raises(verdicts.VerdictError) as e:
        verdicts.parse_verdict(a_verdict("ALREADY_TRACKED",
                                         duplicate_of="the other one"))
    assert any("duplicate_of" in p for p in e.value.problems)


def test_every_problem_is_reported_at_once():
    broken = a_verdict("GAP")
    broken.pop("proposed_fix")
    broken["root_cause"] = "dunno"
    broken["evidence"] = []
    with pytest.raises(verdicts.VerdictError) as e:
        verdicts.parse_verdict(broken, subject="wo-99999999")
    joined = " | ".join(e.value.problems)
    assert len(e.value.problems) >= 4, joined
    for expected in ("proposed_fix", "root_cause", "evidence", "wo-99999999"):
        assert expected in joined


def test_the_settle_headline_names_what_the_user_owes():
    doc = verdicts.parse_verdict(a_verdict("WAITING_ON_USER"))
    headline = verdicts.settle_headline("inv-1234abcd", doc)
    assert "inv-1234abcd" in headline
    assert doc["user_owes"][:20] in headline


def test_the_gap_class_is_required_on_all_four_classifications():
    """Appendix A.3: item 4 asks for the class on EVERY investigation, so excluding
    WAITING_ON_USER and TRANSIENT would delete the "four of these ended with the user
    owing something" signal."""
    for classification in verdicts.CLASSIFICATIONS:
        doc = a_verdict(classification)
        assert verdicts.parse_verdict(doc)["gap_class"] == doc["gap_class"]
        doc.pop("gap_class")
        with pytest.raises(verdicts.VerdictError) as e:
            verdicts.parse_verdict(doc)
        assert any("gap_class" in p for p in e.value.problems), classification


def test_an_unregistered_but_well_formed_gap_class_is_accepted():
    """A.2's two tiers: the verdict checks SHAPE, never membership in `GAP_CLASSES` —
    the first occurrence of a mechanism nobody has named is the normal case."""
    doc = verdicts.parse_verdict(a_verdict("GAP", gap_class="awaiting-signin"))
    assert doc["gap_class"] == "awaiting-signin"
    assert not gaps.registered("awaiting-signin")


def test_a_gap_class_that_is_a_sentence_is_refused():
    with pytest.raises(verdicts.VerdictError) as e:
        verdicts.parse_verdict(
            a_verdict("GAP", gap_class="the panel gave up and never re-derived it"))
    assert any("gap_class" in p for p in e.value.problems)
    # The control: the same document with a slug.
    assert verdicts.parse_verdict(a_verdict("GAP", gap_class="stale-hold"))


def test_a_proposed_fix_without_a_detector_or_a_remedy_is_refused():
    """A.3: they are the investigator's diagnostic contribution and the fix order's
    acceptance criteria, so they are required exactly as every other field there is."""
    assert verdicts.PROPOSED_FIX_FIELDS[-2:] == ("detector", "remedy")
    for name in ("detector", "remedy"):
        fix = dict(a_verdict("GAP")["proposed_fix"])
        fix.pop(name)
        with pytest.raises(verdicts.VerdictError) as e:
            verdicts.parse_verdict(a_verdict("GAP", proposed_fix=fix))
        assert any(f"proposed_fix.{name}" in p for p in e.value.problems)
    # The control: untouched, both fields present.
    doc = verdicts.parse_verdict(a_verdict("GAP"))
    assert doc["proposed_fix"]["detector"] and doc["proposed_fix"]["remedy"]


def test_the_rendered_verdict_leads_with_the_class_and_shows_the_deliverables():
    text = "\n".join(verdicts.render_verdict(verdicts.parse_verdict(a_verdict("GAP"))))
    assert "stale-hold" in text.splitlines()[0]
    assert "detector:" in text
    assert "remedy:" in text
    owed = "\n".join(verdicts.render_verdict(
        verdicts.parse_verdict(a_verdict("WAITING_ON_USER"))))
    assert "stale-hold" in owed.splitlines()[0]
