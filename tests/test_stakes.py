"""A model decides what is high-stakes: the pure module (docs/superpowers/specs/2026-09-25-a-model-decides-what-is-high-stakes.md)"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from jarvis import stakes

ROUTINE_REPLY = json.dumps({"high": False, "category": "none",
                            "reason": "renaming a private helper commits to no act"})


def test_a_well_formed_routine_reply_is_the_one_thing_that_arms():
    verdict = stakes.read_verdict(ROUTINE_REPLY)

    assert verdict.high is False
    assert verdict.category == "none"
    assert verdict.parsed is True
    assert "commits to no act" in verdict.reason


def test_a_well_formed_high_reply_names_its_category():
    verdict = stakes.read_verdict(json.dumps({
        "high": True, "category": "spending-money",
        "reason": "spends real API dollars on a probe"}))

    assert verdict.high is True
    assert verdict.category == "spending-money"
    assert verdict.parsed is True


def test_a_fenced_reply_with_prose_around_it_still_parses():
    """`structured.coerce` tolerates chatty output"""
    verdict = stakes.read_verdict(
        "Here is my judgement.\n\n```json\n" + ROUTINE_REPLY + "\n```\nHope that helps.")

    assert verdict.high is False
    assert verdict.parsed is True


def test_a_verdict_wrapped_in_an_array_is_read_as_that_verdict():
    """`structured.parse_json_object` lifts the object out"""
    verdict = stakes.read_verdict("[" + ROUTINE_REPLY + "]")

    assert verdict.high is False


# -- the fail-closed table

FAIL_CLOSED = {
    "category_absent": json.dumps({"high": False, "reason": "routine"}),
    "category_empty": json.dumps({"high": False, "category": "", "reason": "routine"}),
    "category_misspelled": json.dumps({"high": False, "category": "non",
                                       "reason": "routine"}),
    "category_invented": json.dumps({"high": False, "category": "refactoring",
                                     "reason": "routine"}),
    "high_false_with_a_real_category": json.dumps({
        "high": False, "category": "destroying-data", "reason": "routine"}),
    "high_absent": json.dumps({"category": "none", "reason": "routine"}),
    "high_is_the_string_false": json.dumps({"high": "false", "category": "none",
                                            "reason": "routine"}),
    "high_is_a_number": json.dumps({"high": 0, "category": "none",
                                    "reason": "routine"}),
    "reason_empty_on_a_none_verdict": json.dumps({"high": False, "category": "none",
                                                  "reason": ""}),
    "reason_absent_on_a_none_verdict": json.dumps({"high": False, "category": "none"}),
    "raw_empty": "",
    "raw_whitespace": "   \n  ",
    "raw_not_json": "I think this one is fine, honestly.",
    "raw_json_of_another_shape": json.dumps({"verdict": "approve", "escalate": False}),
    "raw_truncated_json": '{"high": false, "category": "non',
}


@pytest.mark.parametrize("case", sorted(FAIL_CLOSED))
def test_every_malformed_reply_is_high(case):
    verdict = stakes.read_verdict(FAIL_CLOSED[case])

    assert verdict.high is True, f"{case} armed — the allowlist failed open"
    assert verdict.reason, f"{case} held with no reason"


@pytest.mark.parametrize("case", sorted(FAIL_CLOSED))
def test_read_verdict_never_raises(case):
    """A classifier that throws parks the assumption pass that called it"""
    assert isinstance(stakes.read_verdict(FAIL_CLOSED[case]), stakes.Stakes)


@pytest.mark.parametrize("raw", [None, 0, [], {}, object()])
def test_read_verdict_never_raises_on_a_non_string_either(raw):
    assert stakes.read_verdict(raw).high is True  # type: ignore[arg-type]


def test_an_unreadable_reply_says_it_was_unreadable():
    """The pinned NEVER-FABRICATE-A-DEFAULT-FROM-A-FAILURE learning: a hold from a broken reply must say..."""
    verdict = stakes.read_verdict("not json at all")

    assert verdict.high is True
    assert verdict.parsed is False
    assert verdict.reason == stakes.HIGH_UNPARSEABLE
    assert verdict.category == ""


def test_an_unreachable_classifier_says_it_was_unreachable():
    verdict = stakes.unreachable("haiku")

    assert verdict.high is True
    assert verdict.parsed is False
    assert verdict.reason == stakes.HIGH_UNREACHABLE
    assert verdict.category == ""
    assert verdict.model == "haiku"


def test_the_two_failure_reasons_are_different_text():
    """They are different facts — nothing answered, versus something answered unreadably"""
    assert stakes.HIGH_UNREACHABLE != stakes.HIGH_UNPARSEABLE


def test_a_disagreeing_reply_keeps_the_category_it_named():
    """`high: false` with a real category is a model that did not answer the question asked"""
    verdict = stakes.read_verdict(json.dumps({
        "high": False, "category": "publishing", "reason": "just a mention"}))

    assert verdict.high is True
    assert verdict.category == "publishing"
    assert verdict.parsed is True


def test_the_model_that_answered_rides_on_the_verdict():
    verdict = stakes.read_verdict(ROUTINE_REPLY, model="claude-haiku-4-5-20251001")

    assert verdict.model == "claude-haiku-4-5-20251001"


def test_a_high_verdict_with_no_reason_reads_as_no_reason_given():
    """SS3.2 rule 4: empty `reason` is legal on a HIGH verdict"""
    verdict = stakes.read_verdict(json.dumps({"high": True, "category": "publishing",
                                              "reason": ""}))

    assert verdict.high is True
    assert verdict.reason == "no reason given"


# -- the prompt


def test_the_categories_are_the_six_clauses_plus_none():
    assert stakes.STAKES_CATEGORIES == (
        "production-or-live-credentials", "spending-money", "destroying-data",
        "publishing", "legal-or-personal-data", "breaking-change", "none")


def test_the_question_carries_the_assumption_and_nothing_else():
    """SS3.1: no work order, no siblings, no diff"""
    text = "deleted the dead branch of `render_row`"
    prompt = stakes.question(text)

    assert text in prompt
    assert len(prompt) < 1200


def test_the_persona_names_every_category_it_asks_for():
    for category in stakes.STAKES_CATEGORIES:
        assert category in stakes.PERSONA


# -- the synthetic corpus


def synthetic_rows():
    path = (Path(__file__).resolve().parents[1] / "evals" / "data"
            / "stakes_corpus_synthetic.json")
    return json.loads(path.read_text())["rows"]


def test_the_synthetic_corpus_has_the_row_shape_the_eval_reads():
    rows = synthetic_rows()

    assert len(rows) == 24
    assert len({r["id"] for r in rows}) == 24
    for row in rows:
        assert set(row) == {"id", "text", "label", "category", "why", "uncertain"}
        assert row["label"] in ("high", "routine")
        assert row["category"] in stakes.STAKES_CATEGORIES
        assert (row["category"] == "none") is (row["label"] == "routine")
        assert row["uncertain"] is False
        assert row["why"]


def test_the_synthetic_corpus_carries_both_failure_directions():
    """An A/B on a corpus every arm scores 1.0 on proves nothing"""
    from jarvis import autoreview

    rows = synthetic_rows()
    high = [r for r in rows if r["label"] == "high"]
    routine = [r for r in rows if r["label"] == "routine"]

    assert len(high) == len(routine) == 12
    invisible = [r for r in high if not autoreview.high_stakes_marker(r["text"])]
    held = [r for r in routine if autoreview.high_stakes_marker(r["text"])]
    assert len(invisible) == 6, [r["id"] for r in invisible]
    assert len(held) >= 6, [r["id"] for r in held]


def test_the_stakes_verdict_is_frozen():
    with pytest.raises(Exception):
        stakes.read_verdict(ROUTINE_REPLY).high = True  # type: ignore[misc]


# -- what the persona must SAY, and it is calibration, not decoration


def test_the_persona_says_the_judgement_is_about_the_act_not_the_topic():
    """The nine misses are sentences ABOUT a publishing act whose stakes the model read topically"""
    persona = stakes.PERSONA.lower()
    assert "did or is about to do" in persona or "did, or will do" in persona
    assert "commits to nothing" in persona


def test_the_persona_names_settling_a_version_as_publishing():
    """The named misses"""
    persona = stakes.PERSONA.lower()
    for phrase in ("which version ships", "computed bump", "tag", "remote ref",
                   "issue tracker"):
        assert phrase in persona, phrase


def test_the_persona_names_the_live_system_acts_it_used_to_leave_implicit():
    persona = stakes.PERSONA.lower()
    for phrase in ("migration", "backfill", "gate", "exemption", "permission mode"):
        assert phrase in persona, phrase


def test_the_few_shots_are_three_high_three_none_and_none_come_from_the_corpus():
    """A few-shot lifted from the corpus is training on the test set"""
    examples = [line.strip() for line in stakes.PERSONA.splitlines()
                if line.strip().startswith('"') and ' -> ' in line]
    assert len(examples) == 6, examples
    verdicts = [line.rsplit("->", 1)[-1].strip().split()[0] for line in examples]
    assert sorted(verdicts) == ["high", "high", "high", "none", "none", "none"], verdicts

    corpus_text = " ".join(r["text"] for r in synthetic_rows())
    for line in examples:
        sentence = line.split(" -> ")[0].strip().strip('"')
        assert sentence and sentence not in corpus_text, sentence
        assert len(sentence) < 160, sentence


def test_the_prompt_stays_cheap_enough_to_pay_per_assumption():
    """Paid once per pending assumption"""
    assert len(stakes.PERSONA) < 4500, len(stakes.PERSONA)
