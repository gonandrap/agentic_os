"""Knowledge WRITES are attributed to the work order that made them.

Issue #200's second half: reads were attributed from the day `knowledge_reads` existed
and writes were not, so no query could answer "what did this work order change in the
knowledge base". Spec 2026-09-12 §4.
"""

from __future__ import annotations

import pytest

from jarvis import ops
from jarvis.central_store import CentralStore


@pytest.fixture()
def central():
    store = CentralStore()
    yield store
    store.close()


# ------------------------------------------------------------------------- the columns


def test_an_entry_records_the_work_order_that_wrote_it(central):
    row = ops.learn_add("a thing worth remembering", project="p", topic="t",
                        wo_id="wo-1")
    assert central.get_knowledge(row["id"])["wo_id"] == "wo-1"


def test_a_retraction_records_the_work_order_SEPARATELY_from_the_author(central):
    """The interesting case is retracting somebody ELSE's entry — it is what wo-28405ea1
    did — so overwriting `wo_id` would erase who was originally wrong."""
    row = ops.learn_add("call /snap/bin/gh", wo_id="wo-author")
    ops.learn_retract(row["id"], "it breaks gate-grant matching", wo_id="wo-fixer")
    after = central.get_knowledge(row["id"])
    assert after["wo_id"] == "wo-author"
    assert after["retired_by_wo_id"] == "wo-fixer"


def test_a_person_at_a_terminal_attributes_nothing(central):
    """`''`, not a placeholder: it is what every pre-existing row reads as, and it is
    what those rows were."""
    row = ops.learn_add("typed by a human")
    assert central.get_knowledge(row["id"])["wo_id"] == ""


def test_the_query_issue_200_said_did_not_exist(central):
    """Both columns in one answer, because a work order that retracts an entry and
    writes its replacement did TWO things and a reviewer needs to see both."""
    old = ops.learn_add("the old advice", wo_id="wo-old")
    ops.learn_retract(old["id"], "superseded", wo_id="wo-1")
    new = ops.learn_add("the new advice", wo_id="wo-1")
    ops.learn_add("unrelated", wo_id="wo-other")

    got = {r["id"] for r in central.knowledge_by_work_order("wo-1")}
    assert got == {old["id"], new["id"]}


def test_an_empty_work_order_id_matches_nothing(central):
    """Otherwise every entry a human ever typed would be attributed to the next work
    order that asked."""
    ops.learn_add("typed by a human")
    assert central.knowledge_by_work_order("") == []


# --------------------------------------------------------------- the side-effect records


def test_side_effects_of_describes_both_halves_of_a_replacement():
    old = ops.learn_add("the old advice", wo_id="wo-old", topic="paths")
    ops.learn_retract(old["id"], "it breaks gate matching", wo_id="wo-1")
    new = ops.learn_add("the new advice", wo_id="wo-1", topic="paths")

    effects = {e["kind"]: e for e in ops.side_effects_of("wo-1")}
    assert set(effects) == {"knowledge_retracted", "knowledge_added"}
    assert effects["knowledge_retracted"]["id"] == old["id"]
    assert "it breaks gate matching" in effects["knowledge_retracted"]["summary"]
    assert effects["knowledge_added"]["id"] == new["id"]


def test_a_retraction_carries_the_whole_retired_text_not_a_headline():
    """A reviewer asked to judge a retraction cannot do it from a summary line: the
    question is whether the text that was retired deserved to be."""
    body = "line one\n" + "the middle matters\n" * 20 + "line last\n"
    row = ops.learn_add(body, wo_id="wo-author")
    ops.learn_retract(row["id"], "superseded", wo_id="wo-1")
    detail = ops.side_effects_of("wo-1")[0]["detail"]
    assert detail == body


def test_a_work_order_that_touched_no_knowledge_has_no_side_effects():
    assert ops.side_effects_of("wo-quiet") == []


# ------------------------------------------------------------------ the durable/best-effort split


def test_the_entry_still_lands_when_the_work_order_does_not_exist(central):
    """The timeline half is best effort and the knowledge half is not: an entry that was
    written must not be reported as failed because its work order has been deleted."""
    row = ops.learn_add("written by a ghost", wo_id="wo-deleted-long-ago")
    assert central.get_knowledge(row["id"])["wo_id"] == "wo-deleted-long-ago"


def test_side_effects_are_read_from_the_rows_and_not_from_the_timeline():
    """`_record_side_effect` can fail silently; the row cannot. An entry whose event
    never landed is still judged."""
    ops.learn_add("no timeline anywhere", wo_id="wo-no-project")
    assert len(ops.side_effects_of("wo-no-project")) == 1


def test_retract_raises_exactly_what_the_store_raises():
    """The CLI's error handling is unchanged — it catches these two by type."""
    row = ops.learn_add("once", wo_id="wo-1")
    ops.learn_retract(row["id"], "done", wo_id="wo-1")
    with pytest.raises(ValueError):
        ops.learn_retract(row["id"], "again", wo_id="wo-1")
    with pytest.raises(KeyError):
        ops.learn_retract("kn-nope", "reason", wo_id="wo-1")
