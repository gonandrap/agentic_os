"""The gap class registry — Appendix A.2 of docs/superpowers/specs/2026-09-27-investigation-orders.md"""

from __future__ import annotations

import pytest

from jarvis import gaps


def test_the_registry_is_closed_by_this_test():
    assert tuple(gaps.GAP_CLASSES) == gaps.SHIPPED_GAP_CLASSES
    for slug, cls in gaps.GAP_CLASSES.items():
        assert cls.id == slug
        assert gaps.registered(slug)
        assert gaps.get(slug) is cls


def test_a_well_formed_slug_is_accepted_even_when_unregistered():
    assert gaps.checked_slug("awaiting-signin") == "awaiting-signin"
    assert not gaps.registered("awaiting-signin")
    assert gaps.get("awaiting-signin") is None
    for slug in gaps.SHIPPED_GAP_CLASSES:
        assert gaps.checked_slug(slug) == slug


@pytest.mark.parametrize("bad", [
    "",
    "stale",                                  # one word names nothing
    "Stale-Hold",                             # not lowercase
    "stale hold",                             # a phrase, not a slug
    "stale_hold",                             # underscores are not hyphens
    "the-order-is-stuck-in-validating-for",   # six words is a sentence
    "the order is stuck because the panel gave up",
])
def test_a_malformed_slug_is_refused(bad):
    with pytest.raises(gaps.GapClassError) as e:
        gaps.checked_slug(bad)
    assert "gap class slug" in str(e.value)


def test_by_invariant_skips_the_classes_with_no_detector_yet():
    inverse = gaps.by_invariant()
    assert "" not in inverse
    assert inverse == {c.detector: c.id for c in gaps.GAP_CLASSES.values() if c.detector}


def test_the_classes_with_a_detector_are_named():
    """§2.5 of docs/superpowers/specs/2026-10-15-a-precondition-is-a-row-not-prose.md, and issue 1562"""
    armed = {slug for slug, c in gaps.GAP_CLASSES.items() if c.detector}
    assert armed == {"unwired-precondition", "background-task-silence",
                     "blocker-contradicts-record", "designed-wait-undiscounted",
                     "superseded-delivery", "base-chase-livelock",
                     "mention-held-as-act"}
    assert (gaps.by_invariant()["INV-BLOCKER-CONTRADICTS-RECORD"]
            == "blocker-contradicts-record")
    assert (gaps.by_invariant()["INV-RED-RELEASE-UNDISCOUNTED"]
            == "designed-wait-undiscounted")
    assert gaps.GAP_CLASSES["background-task-silence"].detector == "background.live_tasks"
    assert (gaps.by_invariant()["INV-PRECONDITION-UNWIRED"]
            == "unwired-precondition")
    assert gaps.GAP_CLASSES["unwired-precondition"].remedy == gaps.REMEDY_NONE


def test_render_registry_names_every_shipped_class():
    text = gaps.render_registry()
    for slug, cls in gaps.GAP_CLASSES.items():
        assert f"`{slug}`" in text
        assert cls.headline in text
        assert cls.symptom in text
    assert "NONE YET" in text     # the honest state of a class with no detector


def test_render_registry_shows_a_remedy_only_when_there_is_one(monkeypatch):
    armed = gaps.GapClass(id="armed-class", headline="h", symptom="s", detector="INV-X",
                          remedy="nudge", issue_url="", since="")
    monkeypatch.setitem(gaps.GAP_CLASSES, armed.id, armed)
    text = gaps.render_registry()
    assert "Detector: INV-X; remedy: nudge" in text
    assert "remedy: none" not in text
