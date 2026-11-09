"""A PR-body image that renders broken never reaches the panel"""

from __future__ import annotations

import dataclasses
import json
import subprocess

import pytest

from jarvis import evidence, github
from tests.test_precheck_before_the_panel import rounds, submit_fix_order
from tests.test_validation_loop import fleet  # noqa: F401

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 16


def _with_body(monkeypatch, body: str) -> None:
    real = evidence.collect_work_order
    monkeypatch.setattr(evidence, "collect_work_order", lambda *a, **k: dataclasses.replace(
        real(*a, **k), pr={"body": body}))


# -- check_image: what a repo member's browser would see


def test_a_raw_url_a_browser_cannot_fetch_is_broken_and_names_the_blob_form(monkeypatch):
    """PR 7: the raw host carries no github.com session, so a private repo 404s"""
    monkeypatch.setattr(github, "_anonymous_fetch", lambda url: (404, "text/plain"))

    ok, why = github.check_image(
        "https://raw.githubusercontent.com/o/r/abc123/docs/screenshots/a.png")

    assert ok is False
    assert "?raw=true" in why


def test_a_public_raw_url_renders(monkeypatch):
    monkeypatch.setattr(github, "_anonymous_fetch", lambda url: (200, "image/png"))

    assert github.check_image("https://raw.githubusercontent.com/o/r/abc/a.png") == (True, "")


def test_a_blob_raw_url_is_read_as_a_member(monkeypatch):
    seen = []

    def run(argv, **kw):
        seen.append(argv)
        return subprocess.CompletedProcess(argv, 0, PNG, b"")

    monkeypatch.setattr(github.subprocess, "run", run)

    ok, _ = github.check_image("https://github.com/o/r/blob/abc123/docs/a.png?raw=true")

    assert ok is True
    assert "repos/o/r/contents/docs/a.png?ref=abc123" in seen[0]


def test_a_blob_url_that_is_not_an_image_or_not_there_is_broken(monkeypatch):
    monkeypatch.setattr(github.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(
        argv, 0, b"hello", b""))
    assert github.check_image("https://github.com/o/r/blob/abc/a.png?raw=true")[0] is False

    monkeypatch.setattr(github.subprocess, "run", lambda argv, **kw: subprocess.CompletedProcess(
        argv, 1, b"", b"gh: Not Found (HTTP 404)"))
    assert github.check_image("https://github.com/o/r/blob/abc/a.png?raw=true")[0] is False


def test_a_blob_page_without_raw_true_is_html_not_an_image():
    ok, why = github.check_image("https://github.com/o/r/blob/abc/a.png")

    assert ok is False and "?raw=true" in why


@pytest.mark.parametrize("target, patch", [
    ("https://example.com/a.png", ("_anonymous_fetch", lambda url: (0, "URLError: down"))),
    ("https://github.com/o/r/blob/abc/a.png?raw=true",
     ("_member_fetch", lambda *a: (None, "`gh api` failed: HTTP 502"))),
])
def test_an_unreachable_image_is_unchecked_never_broken(monkeypatch, target, patch):
    """kn-40db1828: no verdict from a failure"""
    monkeypatch.setattr(github, *patch)

    check = github.body_image_check(f"![a]({target})")

    assert check.broken == ()
    assert [t for t, _ in check.unchecked] == [target]


# -- the round


def test_a_broken_body_image_sends_the_round_back_without_paying_a_seat(fleet, monkeypatch):  # noqa: F811
    _with_body(monkeypatch, "## Screenshots\n\n![a](docs/screenshots/a.png)\n")
    wo_id, v = submit_fix_order(fleet)
    fleet.drain()

    assert v.calls == []
    [rnd] = rounds(fleet, wo_id)
    assert rnd["outcome"] == "rejected"
    assert "docs/screenshots/a.png" in rnd["reason"]


def test_an_unchecked_image_is_recorded_and_the_panel_still_judges(fleet, monkeypatch):  # noqa: F811
    from jarvis import ops

    target = "https://example.com/a.png"
    _with_body(monkeypatch, f"![a]({target})")
    monkeypatch.setattr(github, "_anonymous_fetch", lambda url: (0, "URLError: down"))
    wo_id, v = submit_fix_order(fleet)
    fleet.drain()

    assert len(v.calls) == 1
    store = fleet.store()
    try:
        [event] = store.events_of_kind(wo_id, "validation_images_unchecked")
    finally:
        store.close()
    assert json.loads(event["payload"])["images"][0]["target"] == target
    assert ops.validation_view(wo_id)["images_unchecked"][0]["round"] == 1
