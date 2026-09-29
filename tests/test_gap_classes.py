"""The gap class registry — Appendix A.2 of
docs/superpowers/specs/2026-09-27-investigation-orders.md.

Two tiers, and only one of them is closed: `GAP_CLASSES` is closed BY THIS FILE, while
`checked_slug` checks shape and never membership. A test that conflated the two would
delete the one case an investigation exists for — the first occurrence of a mechanism
nobody has named yet.
"""

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


def test_render_registry_names_every_shipped_class():
    text = gaps.render_registry()
    for slug, cls in gaps.GAP_CLASSES.items():
        assert f"`{slug}`" in text
        assert cls.headline in text
        assert cls.symptom in text
    assert "NONE YET" in text     # the honest state of a class with no detector
