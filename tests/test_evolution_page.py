"""The dashboard's `/evolution` surface — §9 of
docs/superpowers/specs/2026-09-27-self-evolution.md.

`ops.evolution_report` computes every figure ONCE and both the CLI and this page render
it verbatim. So the tests that matter here are not about arithmetic — there is none to
check — they are about the two ways this page could still lie:

* it could DERIVE a number of its own, and then disagree with `jarvis evolution` about
  the same registry (the hand-built-inconsistent-dict test below is the one that catches
  that);
* it could turn an ABSENT figure into a zero, which reads as a measurement nobody took.

Plus the §9 hard requirement: it must render against an EMPTY registry, because
`uilog.record_error` plus `INV-UI-HEALTHY` turn a 500 into an inbox item and an empty
registry is what every install starts with.
"""

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
    """Everything after `base.html`'s inline stylesheet.

    The assertions below include `"0%" not in …`, and the stylesheet legitimately
    carries `width: 100%` and `color-mix(… 10%, …)`. Scoping to the body keeps the
    assertion about what the PAGE said rather than about the CSS.
    """
    return html.split("</style>", 1)[-1]


def report(**over) -> dict:
    """A complete `evolution_report` dict with nothing in it, for monkeypatching.

    Written out here rather than taken from a real registry so a test can state one
    deliberately wrong figure and nothing else.
    """
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
    """Just the timeline list — the region a links assertion may look at.

    The nav is in the body too and is full of `href`s, so "this entry rendered no link"
    can only be asserted against the entry's own markup.
    """
    after = html.split('id="evolution-timeline"', 1)[-1]
    return after.split("</ul>", 1)[0]


# -- the §9 requirement: an empty registry is the normal case ---------------------------


def test_the_fleet_page_renders_against_an_empty_registry(client):
    res = client.get("/evolution")
    assert res.status_code == 200
    body = body_of(res.text)
    # Not a blank page: an install with no armed rule still has to say WHY there is no
    # mechanical share, which is the whole content of the figure on day one.
    assert "no rule has been armed yet" in body


def test_the_project_page_renders_and_scopes_to_that_project(client):
    res = client.get("/evolution/proj_a")
    assert res.status_code == 200
    assert "proj_a" in body_of(res.text)


def test_the_days_parameter_narrows_the_window(client):
    """`days` widens the page the way `--days` widens the CLI report."""
    res = client.get("/evolution?days=7")
    assert res.status_code == 200
    assert "7 days" in body_of(res.text)


# -- the page prints what the dict says and computes nothing ----------------------------


def test_the_page_renders_the_dicts_share_and_recomputes_none_of_it(client, monkeypatch):
    """THE spec-named test. The dict's `value` is 0.42 while its numerator over its
    denominator is 7/100 — deliberately inconsistent, which a real report can never be.
    A template doing its own arithmetic prints 7%; one rendering the dict prints 42%."""
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


def test_a_rule_with_no_hit_rate_shows_the_note(client, monkeypatch):
    monkeypatch.setattr(ops, "evolution_report", lambda *a, **k: report(
        per_rule=[{"id": "det-1", "gap_class": "stuck_order", "summary": "a summary",
                   "status": "dry_run", "hits": 0, "false_positives": 0,
                   "recurrences": 0, "last_fired": None, "last_cleared": None,
                   "hit_rate": None,
                   "hit_rate_note": "this detector has never fired, so it has no hit rate",
                   "readable": True, "condition_problems": [],
                   "median_cleared_seconds": None,
                   "median_cleared_seconds_note": "0 cleared fires, not a median yet"}]))
    body = body_of(client.get("/evolution").text)
    assert "this detector has never fired, so it has no hit rate" in body
    assert "det-1" in body


def test_a_timeline_entry_with_no_links_renders_no_link(client, monkeypatch):
    """§9: a missing link is ABSENT, never a broken one. The links dict omits the keys
    entirely, so the template may not index one."""
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


def test_a_weaker_half_with_no_side_names_no_side(client, monkeypatch):
    monkeypatch.setattr(ops, "evolution_report", lambda *a, **k: report(
        recurrences={"rows": [], "verdict_counts": {},
                     "weaker_half": {"weaker": None, "missed": 0, "remedy_failed": 0,
                                     "note": "no recurrence has been recorded",
                                     "excluded": {}, "excluded_note": None}}))
    body = body_of(client.get("/evolution").text)
    assert "no recurrence has been recorded" in body
    assert "weaker half: " not in body


# -- the navigation into it -------------------------------------------------------------


def test_the_nav_carries_the_link_and_marks_it_here(client):
    here = client.get("/evolution").text
    assert '<a href="/evolution" class="here">evolution</a>' in here
    elsewhere = client.get("/stuck").text
    assert 'href="/evolution"' in elsewhere
    assert '<a href="/evolution" class="here">evolution</a>' not in elsewhere


def test_the_project_page_links_to_its_own_evolution_report(client):
    body = body_of(client.get("/project/proj_a").text)
    assert 'href="/evolution/proj_a"' in body


# -- read-only, by the work order's scope boundary --------------------------------------


def test_nothing_under_evolution_accepts_a_post(app):
    """The scope boundary says no arming and no rule editing from the page, so the
    absence of a mutating route is a test and not a convention. `jarvis rules retract`
    stays the only way to retract."""
    offenders = [(r.path, sorted(r.methods)) for r in app.routes
                 if getattr(r, "path", "").startswith("/evolution")
                 and "POST" in getattr(r, "methods", set())]
    assert offenders == []
