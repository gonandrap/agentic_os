"""`jarvis rules summary` — the terminal half of the evolution report (§9).

`ops.evolution_report` computes every number ONCE. This file pins the other half of that
contract: the RENDERER derives none of them. The spec-named test below hands
`_print_rules_summary` a dict whose numbers deliberately do not agree with each other and
asserts the printed line is the dict's, not the renderer's — `tests/test_bill.py::
test_jarvis_cost_prints_the_residue_and_the_placeholder_sentence`'s pattern, and the one
test that can catch a surface quietly doing its own arithmetic.

The rest pin ABSENT IS NEVER ZERO at the terminal: every figure in the report is paired
with a `*_note` sentence that is `None` when the figure is present and a SENTENCE when it
is absent, and the renderer prints the sentence — never a digit, never a dash, never "0%".

Spec: docs/superpowers/specs/2026-09-27-self-evolution.md §9.
"""

from __future__ import annotations

import json

import pytest

from jarvis import cli, ops


def _report(**overrides) -> dict:
    """An evolution report with every key `evolution_report` ships, all of it empty.

    HAND-BUILT rather than taken from `ops`: the point of this file is that the renderer
    prints what the dict says, so the dict has to be able to say things `ops` would never
    compute — an inconsistent share above all.
    """
    data = {
        "window": {"days": 90, "since": 1_000_000.0, "until": 1_100_000.0,
                   "week_zone": "Europe/Madrid", "week_zone_note": None},
        "project": None,
        "min_samples": 3,
        "mechanical_share": {"value": None, "note": "nothing yet", "numerator": 0,
                             "denominator": 0, "series": [], "unreadable_projects": []},
        "by_gap_class": [],
        "per_rule": [],
        "recurrences": {"rows": [], "verdict_counts": {"missed": 0, "remedy_failed": 0,
                                                       "not_armed": 0, "unreadable": 0},
                        "weaker_half": {"weaker": None, "missed": 0, "remedy_failed": 0,
                                        "note": "no recurrence yet", "excluded": {},
                                        "excluded_note": ""}},
        "stuck_resolution": [],
        "timeline": [],
        "unreadable": {"projects": [], "detectors": [], "fires": []},
    }
    data.update(overrides)
    return data


def _rule(**overrides) -> dict:
    row = {"id": "dt-1", "gap_class": "stale-panel-hold", "summary": "a hold nobody clears",
           "status": "dry_run", "hits": 4, "false_positives": 1, "recurrences": 0,
           "last_fired": 1_050_000.0, "last_cleared": 1_060_000.0,
           "hit_rate": 0.8, "hit_rate_note": None, "readable": True,
           "condition_problems": [], "median_cleared_seconds": 7200.0,
           "median_cleared_seconds_note": None}
    row.update(overrides)
    return row


# -- the renderer computes nothing ------------------------------------------------------


def test_the_renderer_prints_what_the_dict_says_and_computes_nothing(capsys):
    """THE SPEC-NAMED TEST. 7/100 is not 42%, and the dict says 42%.

    If the renderer ever recomputes `numerator / denominator` this flips to 7%, which is
    `_print_rules_list`'s reason for deriving nothing stated as a failing assertion: two
    surfaces computing the same total is two surfaces that can disagree about it.
    """
    data = _report(mechanical_share={
        "value": 0.42, "note": None, "numerator": 7, "denominator": 100,
        "series": [{"week": "2026-09-21", "numerator": 3, "denominator": 9,
                    "value": 0.91}],
        "unreadable_projects": []})

    cli._print_rules_summary(data)
    out = capsys.readouterr().out

    assert "42" in out
    assert "7%" not in out and "7.0%" not in out
    # The series point is the dict's too: 3/9 is not 91%.
    assert "91" in out and "33%" not in out


def test_an_absent_share_prints_its_sentence_and_no_digit(capsys):
    note = ("no rule has been armed yet, so there is no mechanical share to report — "
            "which is not a share of zero")
    data = _report(mechanical_share={"value": None, "note": note, "numerator": 0,
                                     "denominator": 0, "series": [],
                                     "unreadable_projects": []})

    cli._print_rules_summary(data)
    out = capsys.readouterr().out

    assert note in out
    assert "0%" not in out and "0.0" not in out


def test_a_rule_with_no_hit_rate_prints_the_note_and_no_rate(capsys):
    note = "this detector has never fired, so it has no hit rate — which is not a rate of zero"
    data = _report(per_rule=[_rule(hit_rate=None, hit_rate_note=note)])

    cli._print_rules_summary(data)
    out = capsys.readouterr().out

    assert note in out
    assert "0%" not in out


def test_a_rule_with_too_little_history_prints_the_median_note(capsys):
    note = "1 cleared fire in this window, fewer than the 3 this median needs"
    data = _report(per_rule=[_rule(median_cleared_seconds=None,
                                   median_cleared_seconds_note=note)])

    cli._print_rules_summary(data)
    out = capsys.readouterr().out

    assert note in out


def test_a_stuck_resolution_note_replaces_both_medians(capsys):
    note = ("1 dry-run and 0 armed samples, fewer than the 3 this median needs — not "
            "enough history to compare yet")
    data = _report(stuck_resolution=[{"gap_class": "stale-panel-hold",
                                      "dry_run_median": None, "armed_median": None,
                                      "delta": None, "note": note}])

    cli._print_rules_summary(data)
    out = capsys.readouterr().out

    assert note in out
    # Neither median, and no fabricated delta: "—" would read as a measured zero gap.
    assert "0s" not in out and "0.0" not in out


def test_no_weaker_half_prints_the_note_and_names_neither_side(capsys):
    note = ("no recurrence has been recorded in this window that was either missed or "
            "whose remedy failed, so neither conditions nor remedies can be called the "
            "weaker half")
    data = _report(recurrences={
        "rows": [], "verdict_counts": {"missed": 0, "remedy_failed": 0},
        "weaker_half": {"weaker": None, "missed": 0, "remedy_failed": 0, "note": note,
                        "excluded": {}, "excluded_note": ""}})

    cli._print_rules_summary(data)
    out = capsys.readouterr().out

    assert note in out
    # The NOTE itself contains both words, so the assertion is about the verdict line the
    # renderer would otherwise print: nothing outside the quoted sentence names a side.
    rest = out.replace(note, "")
    assert "weaker half" not in rest


def test_a_timeline_entry_with_no_links_renders_without_raising(capsys):
    data = _report(timeline=[{"ts": 1_050_000.0, "kind": "detector_added", "id": "dt-1",
                              "headline": "a detector for stale-panel-hold was registered",
                              "links": {}}])

    cli._print_rules_summary(data)
    out = capsys.readouterr().out

    assert "a detector for stale-panel-hold was registered" in out
    assert "None" not in out


def test_the_empty_report_says_something_rather_than_printing_nothing(capsys):
    """What every install starts with: an empty registry. Silence would read as a crash."""
    cli._print_rules_summary(_report())
    out = capsys.readouterr().out

    assert out.strip()


# -- the CLI ----------------------------------------------------------------------------


def test_json_emits_the_dict_and_the_plain_form_does_not(monkeypatch, capsys):
    """The `jarvis cost` pairing, built through the REAL parser so the subparser wiring
    is covered rather than assumed."""
    report = _report()
    monkeypatch.setattr(ops, "evolution_report", lambda *a, **k: report)
    parser = cli.build_parser()

    cli.cmd_rules(parser.parse_args(["rules", "summary", "--json"]))
    assert json.loads(capsys.readouterr().out)["window"]["days"] == 90

    cli.cmd_rules(parser.parse_args(["rules", "summary"]))
    plain = capsys.readouterr().out
    assert plain.strip()
    with pytest.raises(json.JSONDecodeError):
        json.loads(plain)


def test_days_and_the_project_reach_ops(monkeypatch, capsys):
    seen = {}

    def _fake(project=None, *, days=90):
        seen["project"], seen["days"] = project, days
        return _report()

    monkeypatch.setattr(ops, "evolution_report", _fake)
    parser = cli.build_parser()
    cli.cmd_rules(parser.parse_args(["rules", "summary", "proj_a", "--days", "7"]))
    capsys.readouterr()

    assert seen == {"project": "proj_a", "days": 7}
    # No project named is the FLEET, and `ops` is told so with None rather than "".
    cli.cmd_rules(parser.parse_args(["rules", "summary"]))
    capsys.readouterr()
    assert seen["project"] is None and seen["days"] == 90
