"""The trimmed-packet A/B's plumbing, proven without spending a cent.

`evals/llm/test_confirm_evidence_ab.py` asks one question: does the trimmed
assumption-confirmation packet judge the same as the full diff? It is gated on
`JARVIS_EVALS_LLM` and never runs here, so its arms, its scorer and its report have
exactly one caller and that caller never runs in CI — the plumbing is the part that rots
(kn-dd0b015a §4, kn-45bae078).

Everything below drives the same code paths with a FAKE transport: no model call, no
network. What it cannot prove is the judgement, which is the one thing the paid run buys.
"""

from __future__ import annotations

import copy
import importlib.util
import json
import statistics
import sys
from pathlib import Path

import pytest

from jarvis import autoreview, catalog, claude_cli, daemon

REPO_ROOT = Path(__file__).resolve().parents[1]
EVAL_PATH = REPO_ROOT / "evals" / "llm" / "test_confirm_evidence_ab.py"

APPROVE = json.dumps({"escalate": False, "verdict": "approve", "stakes": "routine",
                      "reason": "the diff shows the header row unchanged"})
DENY = json.dumps({"escalate": False, "verdict": "deny", "stakes": "routine",
                   "reason": "the export's column order did change"})
ESCALATE = json.dumps({"escalate": True, "verdict": "deny", "stakes": "routine",
                       "reason": "I cannot see the file this is about"})


@pytest.fixture(scope="module")
def ab():
    spec = importlib.util.spec_from_file_location("_confirm_ab_under_test", EVAL_PATH)
    assert spec and spec.loader, f"cannot load {EVAL_PATH}"
    mod = importlib.util.module_from_spec(spec)
    # Registered BEFORE exec: `@dataclass` resolves its string annotations through
    # `sys.modules[cls.__module__]`, which is None for a module loaded by hand.
    sys.modules[spec.name] = mod
    try:
        spec.loader.exec_module(mod)
    finally:
        sys.modules.pop(spec.name, None)
    return mod


@pytest.fixture(scope="module")
def rows(ab) -> list[dict]:
    return ab.load_corpus(ab.SYNTHETIC)["rows"]


def _row(rows: list[dict], row_id: str) -> dict:
    for row in rows:
        if row["id"] == row_id:
            return row
    raise AssertionError(f"{row_id} is not in the synthetic fixture")


def _result(text: str, prompt_chars: int = 0, system_prompt_chars: int = 0):
    return claude_cli.HeadlessResult(
        text=text, usage={"total_cost_usd": 0.01}, session_id="s", model="m",
        prompt_chars=prompt_chars, system_prompt_chars=system_prompt_chars)


def _judgement(ab, *, approve: bool, reached: bool = True, parsed: bool = True):
    return ab.Judgement(approve=approve, reached=reached, parsed=parsed,
                        reason="fabricated", prompt_chars=100,
                        system_prompt_chars=10, latency_ms=1.0)


# -- 1-3: the arms ---------------------------------------------------------------------


def test_the_trim_bites_on_the_over_budget_row(ab, rows):
    """The measurement is worthless if the trimmed arm is not actually smaller."""
    arms = ab.build_arms(_row(rows, "syn-cf01"))

    full_block = arms.full_ev.what_changed()
    trimmed_block = arms.trimmed_ev.what_changed()
    assert len(trimmed_block) < len(full_block)
    assert len(arms.trimmed_ev.diff) <= catalog.DEFAULT_VALIDATION_CONFIRM_DIFF_CHARS
    # The full arm keeps every byte — the hunks are reordered in BOTH arms, so the
    # reordering is part of the frame and not part of what is being measured.
    assert len(arms.full_ev.diff) == len(_row(rows, "syn-cf01")["evidence"]["diff"])
    assert not arms.full_ev.dropped_files
    assert len(arms.full_ev.diff) > 100_000, "sized on kn-1be10a00: q722 was 151,700"

    assert arms.trimmed_ev.diff_truncated
    assert "[diff truncated —" in trimmed_block
    assert not arms.full_ev.diff_truncated
    assert "[diff truncated —" not in full_block


def test_the_hunks_the_assumption_names_go_first(ab, rows):
    """syn-cf01's assumption names the LAST file in diff order. Spending the budget in
    diff order would drop exactly the file the question is about."""
    arms = ab.build_arms(_row(rows, "syn-cf01"))

    assert arms.trimmed_ev.diff.startswith(
        "diff --git a/src/reports_app/exporters/csv_writer.py")
    assert "src/reports_app/api/routes.py" in arms.full_ev.diff


def test_the_arms_are_byte_equal_outside_the_evidence_block(ab, rows):
    for row_id in ("syn-cf01", "syn-cf03", "syn-cf04"):
        arms = ab.build_arms(_row(rows, row_id))
        assert arms.full != arms.trimmed, row_id
        assert ab.frame_of(arms.full, arms.full_ev) == \
            ab.frame_of(arms.trimmed, arms.trimmed_ev), row_id


def test_arm_drift_outside_the_evidence_block_is_refused(ab, rows, monkeypatch):
    """Drift outside the evidence block means the run measured the re-composition
    (kn-fe226ab1). It raises rather than scoring."""
    real = ab.autoreview._confirm_question
    calls = [0]

    def drifting(project, wo, assumption, siblings, ev):
        calls[0] += 1
        text = real(project, wo, assumption, siblings, ev)
        return text + "\n\nPS: be brief." if calls[0] == 2 else text

    monkeypatch.setattr(ab.autoreview, "_confirm_question", drifting)
    with pytest.raises(ab.ArmDrift) as excinfo:
        ab.build_arms(_row(rows, "syn-cf01"))
    assert "outside the evidence block" in str(excinfo.value)


def test_an_under_budget_row_yields_byte_identical_arms(ab, rows):
    arms = ab.build_arms(_row(rows, "syn-cf02"))

    assert arms.full == arms.trimmed
    assert arms.identical
    assert not arms.trimmed_ev.diff_truncated


def test_a_byte_identical_row_is_excluded_from_agreement_and_counted(ab, rows):
    """Nothing was cut, so the row measures nothing — and counting it would inflate the
    agreement number with rows the trim never touched."""
    under = _row(rows, "syn-cf02")
    over = _row(rows, "syn-cf01")

    sc = ab.score([(under, _judgement(ab, approve=True), _judgement(ab, approve=True)),
                   (over, _judgement(ab, approve=True), _judgement(ab, approve=True))])

    assert sc.identical == 1
    assert sc.identical_ids == ["syn-cf02"]
    assert sc.scored == 1
    assert sc.agreement == 1.0


# -- 4: the scorer ---------------------------------------------------------------------


def test_the_scorer_counts_agreement_and_both_flip_directions(ab, rows):
    over = _row(rows, "syn-cf01")

    def other(row_id: str, outcome: str) -> dict:
        row = copy.deepcopy(over)
        row["id"] = row_id
        row["outcome"] = outcome
        return row

    agree = (other("a1", "approved"),
             _judgement(ab, approve=True), _judgement(ab, approve=True))
    unsafe = (other("a2", "escalated"),
              _judgement(ab, approve=False), _judgement(ab, approve=True))
    safe = (other("a3", "approved"),
            _judgement(ab, approve=True), _judgement(ab, approve=False))

    sc = ab.score([agree, unsafe, safe])

    assert sc.scored == 3
    assert sc.agreed == 1
    assert sc.agreement == pytest.approx(1 / 3)
    assert sc.unsafe_flips == ["a2"], "full deny -> trimmed approve is the UNSAFE flip"
    assert sc.safe_flips == ["a3"]
    assert sc.full_vs_recorded == pytest.approx(3 / 3)
    assert sc.trimmed_vs_recorded == pytest.approx(1 / 3)


def test_a_full_deny_to_trimmed_approve_is_unsafe_whatever_was_recorded(ab, rows):
    """The one failure that costs the user something: confirming on evidence the
    reviewer could not see. Recorded outcome does not launder it."""
    row = copy.deepcopy(_row(rows, "syn-cf01"))
    row["outcome"] = "approved"

    sc = ab.score([(row, _judgement(ab, approve=False), _judgement(ab, approve=True))])

    assert sc.unsafe_flips == ["syn-cf01"]
    assert sc.safe_flips == []


# -- 5: a failure and an unreadable reply are different findings, and neither is a
#       verdict ------------------------------------------------------------------------


def test_a_transport_failure_and_an_unreadable_reply_are_different_findings(
        ab, tmp_path, monkeypatch):
    """`neo.parse_verdict` turns unreadable output into `{"escalate": True, …}`, so
    through `read_ruling` an unparseable reply and a genuine escalation look the same.
    The `neo.UNPARSEABLE_PREFIX` on the reason is what tells them apart."""
    def transport(*_a, **_kw):
        raise claude_cli.ClaudeCliError("usage limit reached")

    monkeypatch.setattr(ab.claude_cli, "run_headless_result", transport)
    failed = ab.judge("ASSUMPTION REVIEW — confirm", tmp_path, "m")
    assert (failed.reached, failed.parsed) == (False, False)
    assert not failed.approve

    monkeypatch.setattr(ab.claude_cli, "run_headless_result",
                        lambda *a, **kw: _result("I think it is probably fine"))
    unparsed = ab.judge("ASSUMPTION REVIEW — confirm", tmp_path, "m")
    assert (unparsed.reached, unparsed.parsed) == (True, False)
    assert not unparsed.approve

    monkeypatch.setattr(ab.claude_cli, "run_headless_result",
                        lambda *a, **kw: _result(ESCALATE))
    escalated = ab.judge("ASSUMPTION REVIEW — confirm", tmp_path, "m")
    assert (escalated.reached, escalated.parsed) == (True, True), \
        "a genuine escalation IS a verdict and is scored"


def test_a_failed_or_unreadable_reply_is_never_scored_as_an_approve(ab, rows):
    over = _row(rows, "syn-cf01")

    def other(row_id: str) -> dict:
        row = copy.deepcopy(over)
        row["id"] = row_id
        return row

    sc = ab.score([
        (other("u1"), _judgement(ab, approve=True),
         _judgement(ab, approve=False, reached=True, parsed=False)),
        (other("u2"), _judgement(ab, approve=False, reached=False, parsed=False),
         _judgement(ab, approve=True)),
        (other("u3"), _judgement(ab, approve=True), _judgement(ab, approve=True))])

    assert sc.unparsed == 1 and sc.unparsed_ids == ["u1"]
    assert sc.failed == 1 and sc.failed_ids == ["u2"]
    assert sc.scored == 1, "only the row where both arms returned a verdict is scored"
    assert sc.unsafe_flips == [], "a call that decided nothing is not a flip"
    assert sc.agreement == 1.0


# -- 6: the reported size is the transport's number ------------------------------------


def test_the_reported_arm_size_is_the_transports_prompt_chars(ab, rows, tmp_path,
                                                              monkeypatch):
    """NEVER `len(prompt)`. The saving is read off the same columns `jarvis cost` shows
    (spec §3), so a re-measurement here would report a different number from the OS."""
    monkeypatch.setattr(
        ab.claude_cli, "run_headless_result",
        lambda prompt, **kw: _result(APPROVE, prompt_chars=4242,
                                     system_prompt_chars=777))

    judged = ab.judge("ASSUMPTION REVIEW — confirm", tmp_path, "m")
    assert judged.prompt_chars == 4242 != len("ASSUMPTION REVIEW — confirm")
    assert judged.system_prompt_chars == 777

    row = _row(rows, "syn-cf01")
    results = [ab.RowResult(row=row, arms=ab.build_arms(row),
                            full=judged, trimmed=judged)]
    text = "\n".join(ab.report_lines(ab.SYNTHETIC, results, ab.score(
        [(row, judged, judged)])))
    assert "4,242" in text, text
    assert "777" in text, text


def test_a_failed_call_is_not_folded_into_the_size_report_as_a_zero(ab, tmp_path,
                                                                    monkeypatch):
    """A size that could not be read and a size that is zero are different answers
    (kn-a61f5183). One failure dragging the full arm's median down understates the
    saving with a number from a call that never happened."""
    calls = [0]

    def transport(prompt, **kw):
        calls[0] += 1
        if calls[0] == 1:  # syn-cf01's full arm: the biggest prompt in the corpus
            raise claude_cli.ClaudeCliError("usage limit reached")
        return _result(APPROVE, prompt_chars=len(prompt), system_prompt_chars=900)

    monkeypatch.setattr(ab.claude_cli, "run_headless_result", transport)
    corpus = ab.load_corpus(ab.SYNTHETIC)
    results = ab.run_corpus(corpus["rows"], tmp_path, "m")

    survived = sorted(r.full.prompt_chars for r in results if r.full.reached)
    assert len(survived) == len(results) - 1 and 0 not in survived
    expected = int(statistics.median(survived))

    median, total, _system, counted = ab._sizes(results, "full")
    assert median == expected, "the failed call was folded in as a zero"
    assert total == sum(survived)
    assert counted == len(survived)

    text = "\n".join(ab.report_lines(corpus["path"], results,
                                     ab.score([(r.row, r.full, r.trimmed)
                                               for r in results])))
    assert f"median {expected:,}" in text, text
    assert f"over {len(survived)} calls" in text, text
    assert f"over {len(results)} calls" in text, "the trimmed arm lost nothing"


def test_the_eval_leaves_the_reviewer_no_tools(ab, tmp_path, monkeypatch):
    """A reviewer that can read the repository is a different reviewer from the one
    these numbers measure — the daemon's own pin."""
    seen: dict = {}

    def transport(prompt, **kw):
        seen.update(kw)
        return _result(APPROVE)

    monkeypatch.setattr(ab.claude_cli, "run_headless_result", transport)
    ab.judge("ASSUMPTION REVIEW — confirm", tmp_path, "m")

    assert seen["tools"] == "", f"the eval left the reviewer its tools: {seen!r}"
    assert seen["system_prompt"] == autoreview.ASSUMPTION_REVIEWER_PERSONA
    assert seen["cwd"] == tmp_path


# -- 7: the fixture --------------------------------------------------------------------


def test_the_fixture_loads_and_satisfies_its_own_shape_contract(ab, rows):
    assert len(rows) >= 3
    for row in rows:
        assert row["outcome"] in ("approved", "escalated"), row["id"]
        assert row["assumption"]["content"] and row["work_order"]["result_summary"]
        assert row["evidence"]["files"], row["id"]

    over = [r for r in rows
            if len(r["evidence"]["diff"]) > catalog.DEFAULT_VALIDATION_CONFIRM_DIFF_CHARS]
    under = [r for r in rows
             if len(r["evidence"]["diff"])
             <= catalog.DEFAULT_VALIDATION_CONFIRM_DIFF_CHARS]
    assert over, "no over-budget row: the cut would never happen"
    assert under, "no under-budget row: the byte-identical exclusion is never exercised"
    assert max(len(r["evidence"]["diff"]) for r in over) > 100_000


def test_the_arms_read_the_two_shipped_limits_and_not_a_number_beside_them(ab):
    assert ab.FULL_LIMIT == daemon.CONFIRM_COLLECT_CHARS
    assert ab.TRIMMED_LIMIT == catalog.DEFAULT_VALIDATION_CONFIRM_DIFF_CHARS


# -- the whole flow, against a canned model --------------------------------------------


def test_the_whole_run_and_report_works_against_a_canned_model(ab, tmp_path,
                                                               monkeypatch):
    """Arm build, call, `neo.parse_verdict`, `autoreview.read_ruling`, scoring and the
    report — everything except the judgement. The configuration arguments of a paid eval
    have one caller and it never runs in CI (kn-dd0b015a §4)."""
    seen: list[int] = []

    def transport(prompt, **kw):
        seen.append(len(prompt))
        # syn-cf03's trimmed arm denies where its full arm approves: a SAFE flip, so the
        # run exercises a disagreement without tripping the unsafe assertion.
        trimmed = "[diff truncated —" in prompt
        text = DENY if (trimmed and "wo-cf030003" in prompt) else APPROVE
        return _result(text, prompt_chars=len(prompt), system_prompt_chars=900)

    monkeypatch.setattr(ab.claude_cli, "run_headless_result", transport)
    corpus = ab.load_corpus(ab.SYNTHETIC)

    results = ab.run_corpus(corpus["rows"], tmp_path, "m")
    sc = ab.score([(r.row, r.full, r.trimmed) for r in results])
    lines = ab.report_lines(corpus["path"], results, sc)

    assert len(seen) == 2 * len(corpus["rows"]), "two arms per row, every row called"
    assert sc.identical == 1 and sc.identical_ids == ["syn-cf02"]
    assert sc.scored == len(corpus["rows"]) - 1
    assert sc.unsafe_flips == []
    assert sc.safe_flips == ["syn-cf03"]
    assert sc.unparsed == 0 and sc.failed == 0

    text = "\n".join(lines)
    assert "confirm_corpus_synthetic.json" in text
    assert "UNSAFE flips" in text and "safe flips" in text
    assert "byte-identical" in text and "agreement" in text
    assert f"{ab.TRIMMED_LIMIT:,}" in text and f"{ab.FULL_LIMIT:,}" in text
    # The saving is the other half of the finding: agreement alone cannot say whether the
    # trim bought anything.
    assert "saving" in text


def test_the_assertion_fails_loudly_on_an_unsafe_flip(ab, rows):
    """The one assertion this eval makes. A green scorecard over an unsafe flip is the
    failure mode the whole measurement exists to catch."""
    row = copy.deepcopy(_row(rows, "syn-cf01"))
    sc = ab.score([(row, _judgement(ab, approve=False), _judgement(ab, approve=True))])

    with pytest.raises(AssertionError) as excinfo:
        ab.test_the_trimmed_packet_never_turns_a_refusal_into_a_confirmation(
            {"score": sc, "results": [], "corpus": ab.SYNTHETIC})
    assert "syn-cf01" in str(excinfo.value)
