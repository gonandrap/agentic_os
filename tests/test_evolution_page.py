"""The dashboard's `/evolution` surface — §9 of docs/superpowers/specs/2026-09-27-self-evolution.md"""

from __future__ import annotations

import pytest

pytest.importorskip("fastapi")
from fastapi.testclient import TestClient  # noqa: E402

from jarvis import ops  # noqa: E402
from jarvis.ui.app import create_app  # noqa: E402


@pytest.fixture()
def app():
    return create_app()


@pytest.fixture()
def client(jarvis_home, fake_claude, catalog_file, app):
    ops.start_os(str(catalog_file), foreground=True)
    return TestClient(app, follow_redirects=False)


def body_of(html: str) -> str:
    """Everything after `base.html`'s inline stylesheet"""
    return html.split("</style>", 1)[-1]


def report(**over) -> dict:
    """A complete `evolution_report` dict with nothing in it, for monkeypatching"""
    base = {
        "window": {"days": 90, "since": 1.0, "until": 2.0,
                   "week_zone": "Europe/Madrid", "week_zone_note": None},
        "project": None,
        "min_samples": 5,
        "mechanical_share": {"value": None, "note": "nothing yet", "numerator": 0,
                             "denominator": 0, "series": [], "unreadable_projects": []},
        "by_gap_class": [],
        "per_rule": [],
        "recurrences": {"rows": [], "verdict_counts": {},
                        "weaker_half": {"weaker": None, "missed": 0, "remedy_failed": 0,
                                        "note": "neither half", "excluded": {},
                                        "excluded_note": None}},
        "stuck_resolution": [],
        "timeline": [],
        "unreadable": {"projects": [], "detectors": [], "fires": []},
    }
    base.update(over)
    return base


def timeline_block(html: str) -> str:
    """Just the timeline list — the region a links assertion may look at"""
    after = html.split('id="evolution-timeline"', 1)[-1]
    return after.split("</ul>", 1)[0]


# -- the §9 requirement: an empty registry is the normal case


def test_the_fleet_page_renders_against_an_empty_registry(client):
    res = client.get("/evolution")
    assert res.status_code == 200
    body = body_of(res.text)
    # Not a blank page: an install with no armed rule still has to say WHY there is no mechanical share
    assert "no rule has been armed yet" in body


def test_the_project_page_renders_and_scopes_to_that_project(client):
    res = client.get("/evolution/proj_a")
    assert res.status_code == 200
    assert "proj_a" in body_of(res.text)


def test_an_install_with_an_investigation_and_no_armed_rule_shows_no_percent(client):
    """The ordinary install, end to end: one investigation, nothing armed"""
    from jarvis.project_store import ProjectStore

    project_store = ProjectStore(ops.registered_project_paths()["proj_a"])
    try:
        project_store.create_feature_order("why is wo-1 stuck",
                                           metadata={ops.SUBJECT_KEY: "wo-1"},
                                           kind="investigation")
    finally:
        project_store.close()

    body = body_of(client.get("/evolution").text)

    assert ops._NO_MECHANICAL_SHARE_NOTE in body
    assert "0%" not in body


def test_an_unknown_project_is_a_404_and_not_an_empty_report(client):
    """A page for a name that does not exist would read as a project the OS has learned nothing about"""
    res = client.get("/evolution/no_such_project")

    assert res.status_code == 404


def test_the_days_parameter_narrows_the_window(client):
    """`days` sizes the page's window the way `--days` sizes the CLI report's: 7 narrows it to a week"""
    res = client.get("/evolution?days=7")
    assert res.status_code == 200
    assert "7 days" in body_of(res.text)


def test_the_filter_bars_own_submission_renders(client):
    """The exact URL the filter bar submits with 'every days' picked was a 422 (wo-522ea53a)"""
    res = client.get("/evolution?project=proj_a&days=&firing=")
    assert res.status_code == 200
    assert "90 days" in body_of(res.text)


# -- the page prints what the dict says and computes nothing


def test_the_page_renders_the_dicts_share_and_recomputes_none_of_it(client, monkeypatch):
    """THE spec-named test"""
    monkeypatch.setattr(ops, "evolution_report", lambda *a, **k: report(
        mechanical_share={"value": 0.42, "note": None, "numerator": 7,
                          "denominator": 100, "series": [], "unreadable_projects": []}))
    body = body_of(client.get("/evolution").text)
    assert "42%" in body
    assert "7%" not in body
    assert "7.0" not in body


def test_an_absent_share_prints_the_sentence_and_no_digit(client, monkeypatch):
    note = "no rule has been armed yet, so there is no mechanical share to report"
    monkeypatch.setattr(ops, "evolution_report", lambda *a, **k: report(
        mechanical_share={"value": None, "note": note, "numerator": 0, "denominator": 0,
                          "series": [], "unreadable_projects": []}))
    body = body_of(client.get("/evolution").text)
    assert note in body
    assert "0%" not in body
    assert "0.0" not in body


def _rule(**over) -> dict:
    """One `per_rule` row of a detector that never fired"""
    return {"id": "det-1", "gap_class": "stuck_order", "summary": "a summary",
            "status": "dry_run", "hits": 0, "false_positives": 0,
            "recurrences": 0, "last_fired": None, "last_cleared": None,
            "hit_rate": None,
            "hit_rate_note": "this detector has never fired, so it has no hit rate",
            "readable": True, "condition_problems": [],
            "median_cleared_seconds": None,
            "median_cleared_seconds_note": "0 cleared fires, not a median yet", **over}


def test_a_rule_with_no_hit_rate_shows_the_note(client, monkeypatch):
    """The note left the table for the rule's own page (fo-744c42a5 §5); the list folds it as quiet"""
    monkeypatch.setattr(ops, "evolution_report", lambda *a, **k: report(per_rule=[_rule()]))
    body = body_of(client.get("/evolution").text)
    quiet = body.split('id="quiet-detectors"', 1)[1].split("</details>", 1)[0]
    assert 'href="/evolution/rule/det-1"' in quiet
    detail = body_of(client.get("/evolution/rule/det-1").text)
    assert "this detector has never fired, so it has no hit rate" in detail
    assert "a summary" in detail


def test_an_unknown_rule_is_a_404(client, monkeypatch):
    monkeypatch.setattr(ops, "evolution_report", lambda *a, **k: report(per_rule=[_rule()]))
    assert client.get("/evolution/rule/det-nope").status_code == 404


def test_a_rule_page_shows_what_it_filed_and_its_history(client, monkeypatch):
    monkeypatch.setattr(ops, "evolution_report", lambda *a, **k: report(
        per_rule=[_rule(hits=3, last_fired=1.0)],
        by_gap_class=[{"gap_class": "stuck_order", "total": 3,
                       "weeks": [{"week": "2026-10-19", "count": 3}]}],
        timeline=[{"ts": 1.0, "kind": "detector_added", "id": "det-1",
                   "headline": "a detector was registered", "links": {"fix_wo": "wo-9"}},
                  {"ts": 1.0, "kind": "detector_added", "id": "det-2",
                   "headline": "another detector", "links": {}}]))
    listing = body_of(client.get("/evolution").text)
    assert 'id="rule-det-1"' in listing.split('id="quiet-detectors"')[0]
    detail = body_of(client.get("/evolution/rule/det-1?project=").text)
    assert "2026-10-19" in detail and 'href="/go/wo-9"' in detail
    assert "another detector" not in detail


def test_the_firing_filter_narrows_the_rules(client, monkeypatch):
    monkeypatch.setattr(ops, "evolution_report", lambda *a, **k: report(
        per_rule=[_rule(), _rule(id="det-2", hits=2, last_fired=1.0)]))
    body = body_of(client.get("/evolution?firing=firing").text)
    assert "det-2" in body and "det-1" not in body


def test_a_timeline_entry_with_no_links_renders_no_link(client, monkeypatch):
    """§9: a missing link is ABSENT, never a broken one"""
    monkeypatch.setattr(ops, "evolution_report", lambda *a, **k: report(
        timeline=[{"ts": 1.0, "kind": "gap_class_first_seen", "id": "stuck_order",
                   "headline": "the OS recognised stuck_order for the first time",
                   "links": {}}]))
    res = client.get("/evolution")
    assert res.status_code == 200
    block = timeline_block(body_of(res.text))
    assert "the OS recognised stuck_order for the first time" in block
    assert "href" not in block


def test_a_timeline_entry_renders_only_the_links_it_has(client, monkeypatch):
    monkeypatch.setattr(ops, "evolution_report", lambda *a, **k: report(
        timeline=[{"ts": 1.0, "kind": "detector_added", "id": "det-2",
                   "headline": "a detector was registered",
                   "links": {"pr": "https://example.invalid/pr/1"}}]))
    block = timeline_block(body_of(client.get("/evolution").text))
    assert "https://example.invalid/pr/1" in block
    assert "issue" not in block


def test_a_stuck_resolution_row_with_a_note_shows_the_note(client, monkeypatch):
    monkeypatch.setattr(ops, "evolution_report", lambda *a, **k: report(
        stuck_resolution=[{"gap_class": "stuck_order", "dry_run_median": None,
                           "armed_median": None, "delta": None,
                           "note": "2 dry-run and 0 armed samples, not enough history"}]))
    body = body_of(client.get("/evolution").text)
    assert "2 dry-run and 0 armed samples, not enough history" in body


def _delta_page(client, monkeypatch, delta: float) -> str:
    monkeypatch.setattr(ops, "evolution_report", lambda *a, **k: report(
        stuck_resolution=[{"gap_class": "stuck_order", "dry_run_median": 600.0,
                           "armed_median": 420.0, "delta": delta, "note": None,
                           "dry_run_samples": 5, "armed_samples": 5}]))
    return body_of(client.get("/evolution").text)


def test_a_negative_delta_renders_differently_from_a_positive_one(client, monkeypatch):
    """The sign IS the finding: -3m is arming working, +3m is arming making it worse"""
    better = _delta_page(client, monkeypatch, -180.0)
    assert "-3m" in better
    assert "+3m" not in better

    worse = _delta_page(client, monkeypatch, 180.0)
    assert "+3m" in worse
    assert "-3m" not in worse


def test_a_weaker_half_with_no_side_names_no_side(client, monkeypatch):
    monkeypatch.setattr(ops, "evolution_report", lambda *a, **k: report(
        recurrences={"rows": [], "verdict_counts": {},
                     "weaker_half": {"weaker": None, "missed": 0, "remedy_failed": 0,
                                     "note": "no recurrence has been recorded",
                                     "excluded": {}, "excluded_note": None}}))
    body = body_of(client.get("/evolution").text)
    assert "no recurrence has been recorded" in body
    assert "weaker half: " not in body


def test_no_recurrence_renders_the_sentence_and_no_verdict_counts(client, monkeypatch):
    """Same condition as the CLI: an empty `rows` means no counts line at all"""
    monkeypatch.setattr(ops, "evolution_report", lambda *a, **k: report(
        recurrences={"rows": [],
                     "verdict_counts": {"missed": 0, "remedy_failed": 0,
                                        "not_armed": 0, "unreadable": 0},
                     "weaker_half": {
                         "weaker": None, "missed": 0, "remedy_failed": 0,
                         "note": "no recurrence has been recorded in this window",
                         "excluded": {"unreadable": 0, "not_armed": 0},
                         "excluded_note": "counted here and kept out"}}))
    body = body_of(client.get("/evolution").text)

    assert "no recurrence has been recorded in this window" in body
    for rendered in ("missed 0", "remedy_failed 0", "not_armed 0", "unreadable 0",
                     "counted here and kept out"):
        assert rendered not in " ".join(body.split())


def test_a_recurrence_row_brings_the_verdict_counts_back(client, monkeypatch):
    monkeypatch.setattr(ops, "evolution_report", lambda *a, **k: report(
        recurrences={"rows": [{"ts": 1.0, "gap_class": "stuck_order",
                               "verdict": "missed", "order_id": "wo-1",
                               "note": "n", "filed_note": "f"}],
                     "verdict_counts": {"missed": 1},
                     "weaker_half": {
                         "weaker": "conditions", "missed": 1, "remedy_failed": 0,
                         "note": "conditions are the weaker half",
                         "excluded": {"unreadable": 0},
                         "excluded_note": "counted here and kept out"}}))
    body = " ".join(body_of(client.get("/evolution").text).split())

    assert "missed 1" in body
    assert "counted here and kept out" in body


# -- the navigation into it


def test_the_nav_carries_the_link_and_marks_it_here(client):
    here = client.get("/evolution").text
    assert '<a href="/evolution" class="here">evolution</a>' in here
    elsewhere = client.get("/stuck").text
    assert 'href="/evolution"' in elsewhere
    assert '<a href="/evolution" class="here">evolution</a>' not in elsewhere


def test_the_project_page_links_to_its_own_evolution_report(client):
    body = body_of(client.get("/project/proj_a").text)
    assert 'href="/evolution/proj_a"' in body


# -- read-only, by the work order's scope boundary


def test_nothing_under_evolution_accepts_a_post(app):
    """The scope boundary says no arming and no rule editing from the page"""
    offenders = [(r.path, sorted(r.methods)) for r in app.routes
                 if getattr(r, "path", "").startswith("/evolution")
                 and "POST" in getattr(r, "methods", set())]
    assert offenders == []
