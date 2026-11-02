"""#1366: the rejected alternative is not the act, and `validation.high_stakes_hold: neo`"""

from __future__ import annotations

import pytest

from jarvis import autoreview
from jarvis.catalog import ValidationConfig
from jarvis.stakes import Stakes

# The production row from painforwisdom, verbatim
ARCHIVE = ("auto-memory dir is archived with mv to memory.archived-2026-10-30, never "
           "deleted, and only after every migrated entry is confirmed in jarvis learn. "
           "Rejected: rm of the memory dir (irreversible).")


def test_the_production_row_clears_the_default_net():
    """The wide net still holds it on 'never deleted', a word of the CHOSEN clause"""
    assert autoreview.high_stakes_marker(ARCHIVE, tightened=True) == ""


@pytest.mark.parametrize("tightened", [False, True])
@pytest.mark.parametrize("text,held", [
    ("Kept the reversible move. Rejected alternative: an irreversible purge.", False),
    # The act the worker CHOSE is still held, before or after its rejected clause
    ("Rejected: a soft delete. Chose an irreversible purge of the archive.", True),
    ("Purged the archive irreversibly. Rejected: keeping it.", True),
    # Fails CLOSED: no terminator, a comma, a connective, or mid-sentence strips nothing
    ("Rejected: keep it\nPurged the old archive.", True),
    ("Rejected: a soft delete, so purged the archive irreversibly", True),
    ("Rejected: a soft delete, so purged the archive irreversibly.", True),
    ("The plan was rejected: we drop the prod table", True),
    ("The plan was rejected: we drop the prod table.", True),
    ("Rejected: keeping it and then purged the archive.", True),
])
def test_the_rejected_clause_is_not_matched(tightened, text, held):
    assert bool(autoreview.high_stakes_marker(text, tightened=tightened)) is held


def _decide(hold, **kw):
    cfg = ValidationConfig(enabled=True, auto_review=True, high_stakes_hold=hold)
    a = {"id": 1, "n": 4, "status": "pending",
         "content": "The coach prod model pin is in draft PR #78; left out of scope."}
    return autoreview.decide(a, {"status": "needs_review"}, cfg, **kw)


@pytest.mark.parametrize("stakes", [None, Stakes(high=True, category="", reason="x",
                                                 model="regex-tightened")])
def test_hold_user_keeps_the_hold_and_neo_puts_it_to_neo(stakes):
    assert _decide("user", stakes=stakes).code == autoreview.HELD_HIGH_STAKES
    assert _decide("neo", stakes=stakes).armed
